"""Folder-reader compatibility and complete pre-upload source accounting."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import folder_upload
from immich_ctl.folder_compat import JSON_LIMIT, native_sidecar_issues, sidecar_state
from immich_ctl.takeout_reconcile import reconcile_report
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

MARKER = "immich-go version:0.32.0"
SELECTION = (date(2023, 1, 1), date(2023, 12, 31))
takeout = load_script("immich-takeout")


def native(day="2023-01-01", **fields):
    return {"software": MARKER, "dateTaken": day + "T00:00:00Z", **fields}


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "takeout.zip"
        self.folder = self.root / "extracted"
        self.folder.mkdir()
        self.config = self.root / "account.ini"
        self.config.write_text("[immich]\nserver=https://photos.example\napi_key=synthetic\n")
        self.log = self.root / "upload.log"
        self.entries = {
            "Photos from 2023/photo.jpg": b"photo",
            "Photos from 2023/photo.jpg.json": sidecar("photo.jpg"),
        }
        self.media = self.local("photo.jpg", b"photo", native())
        self.write()

    def write(self):
        write_zip(self.archive, self.entries)

    def local(self, name, content, metadata):
        media = self.folder / name
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(content)
        if metadata is not None:
            Path(str(media) + ".JSON").write_text(json.dumps(metadata))
        return media

    def report(self, selection=SELECTION):
        return reconcile_report([self.archive], [self.folder], selection)

    def test_each_year_reports_conflicts_with_identical_content_outside_its_selection(self):
        self.entries.update(
            {
                "Photos from 2022/other.jpg": b"photo",
                "Photos from 2022/other.jpg.json": sidecar("other.jpg", "other", "1640995200"),
            }
        )
        self.write()
        before = self.archive.read_bytes()
        for year in (2022, 2023):
            with self.subTest(year=year):
                Path(str(self.media) + ".JSON").write_text(json.dumps(native(f"{year}-01-01")))
                report = self.report((date(year, 1, 1), date(year, 12, 31)))
                self.assertEqual(report["selected"]["files"], 1)
                self.assertEqual(report["outside_date_files"], 1)
                self.assertEqual(report["selected"]["needs_review"], 1)
                self.assertFalse(report["metadata_and_albums_validated"])
                self.assertIn(
                    "identical media has conflicting source dateTaken", report["files"][0]["issues"]
                )
                self.assertEqual(len(report["cross_date_conflicts"]), 1)
                evidence = report["cross_date_conflicts"][0]["files"]
                self.assertEqual({row["scope"] for row in evidence}, {"selected", "outside_date"})
                self.assertEqual(
                    {row["path"] for row in evidence},
                    {
                        "Photos from 2023/photo.jpg",
                        "Photos from 2022/other.jpg",
                    },
                )
        self.assertEqual(before, self.archive.read_bytes())

    def test_equal_size_different_content_in_other_year_does_not_create_a_conflict(self):
        self.entries.update(
            {
                "Photos from 2022/other.jpg": b"other",
                "Photos from 2022/other.jpg.json": sidecar("other.jpg", "other", "1640995200"),
            }
        )
        self.write()
        report = self.report()
        self.assertEqual(report["cross_date_conflicts"], [])
        self.assertEqual(report["unexpected_local_files"], [])
        self.assertTrue(report["metadata_and_albums_validated"])

    def test_missing_album_metadata_remains_uncertain_across_all_copies(self):
        self.entries.update(
            {"Trip/photo.jpg": b"photo", "Trip/photo.jpg.json": sidecar("photo.jpg")}
        )
        self.write()
        report = self.report()
        self.assertEqual(report["possible_album_folders"], ["Trip"])
        self.assertEqual(report["selected"]["needs_review"], 2)
        self.assertFalse(report["metadata_and_albums_validated"])
        for row in report["files"]:
            self.assertEqual(row["albums"], [])
            self.assertEqual(row["possible_album_folders"], ["Trip"])
            self.assertTrue(any("lack metadata" in i for i in row["issues"]))

    def test_standard_and_special_folders_are_not_invented_as_albums(self):
        for folder in ("Photos from 2023", "Archive", "Trash", "Failed videos", "."):
            with self.subTest(folder=folder):
                prefix = "" if folder == "." else folder + "/"
                self.entries = {
                    prefix + "photo.jpg": b"photo",
                    prefix + "photo.jpg.json": sidecar("photo.jpg"),
                }
                self.write()
                report = self.report()
                self.assertEqual(report["possible_album_folders"], [])
                self.assertTrue(report["metadata_and_albums_validated"])

    def test_unrelated_local_media_with_or_without_json_prevents_a_clean_report(self):
        for metadata in (None, native()):
            with self.subTest(metadata=metadata):
                extra = self.local("unrelated.jpg", b"unrelated", metadata)
                report = self.report()
                self.assertEqual(report["selected"]["present"], 1)
                self.assertFalse(report["metadata_and_albums_validated"])
                self.assertEqual(len(report["local_media"]), 2)
                self.assertEqual(len(report["unexpected_local_files"]), 1)
                finding = report["unexpected_local_files"][0]
                self.assertEqual(finding["classification"], "not_in_export")
                self.assertEqual(finding["path"], "unrelated.jpg")
                self.assertEqual(finding["source_paths"], [])
                extra.unlink()
                Path(str(extra) + ".JSON").unlink(missing_ok=True)

    def test_outside_excluded_and_unresolved_local_files_are_distinguished(self):
        self.entries.update(
            {
                "Photos from 2022/old.jpg": b"old",
                "Photos from 2022/old.jpg.json": sidecar("old.jpg", "old", "1640995200"),
                "Photos from 2023/trashed.jpg": b"trashed",
                "Photos from 2023/trashed.jpg.json": sidecar("trashed.jpg", "trash")
                | {"trashed": True},
                "Photos from 2023/unpaired.jpg": b"unpaired",
            }
        )
        self.write()
        self.local("old.jpg", b"old", native("2022-01-01"))
        self.local("trashed.jpg", b"trashed", native())
        self.local("unpaired.jpg", b"unpaired", None)
        report = self.report()
        self.assertEqual(
            {r["path"]: r["classification"] for r in report["unexpected_local_files"]},
            {
                "old.jpg": "outside_date",
                "trashed.jpg": "excluded",
                "unpaired.jpg": "unresolved_source",
            },
        )
        self.assertFalse(report["metadata_and_albums_validated"])

    def test_reconciliation_rejects_missing_and_escaped_version_markers(self):
        sidecar_path = Path(str(self.media) + ".JSON")
        for payload in (
            json.dumps({"dateTaken": "2023-01-01T00:00:00Z"}),
            json.dumps(native()).replace("version", r"ver\u0073ion"),
        ):
            with self.subTest(payload=payload):
                sidecar_path.write_text(payload)
                row = self.report()["files"][0]
                self.assertFalse(row["metadata_verified"])
                self.assertEqual(row["status"], "needs_review")
                self.assertIn(
                    "missing immich-go version marker", row["local_matches"][0]["sidecar"]
                )

    def test_upload_preflight_rejects_bad_names_or_sidecars_without_logs_or_tool_calls(self):
        for name, metadata in (
            ("photo.jpg", {"dateTaken": "2023-01-01T00:00:00Z"}),
            ("holiday[1].jpg", native()),
            ("album[1]/photo.jpg", native()),
            ("question?.jpg", native()),
            ("star*.jpg", native()),
            ("slash\\name.jpg", native()),
            ("photo.jpg", None),
        ):
            with self.subTest(name=name, metadata=metadata):
                self.media.unlink(missing_ok=True)
                Path(str(self.media) + ".JSON").unlink(missing_ok=True)
                bad = self.local(name, b"photo", metadata)
                before = {str(p): p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
                with (
                    patch.object(folder_upload.shutil, "which", return_value="immich-go"),
                    patch.object(folder_upload, "run_upload") as tool,
                ):
                    for dry_run in (False, True):
                        with self.assertRaises(folder_upload.FolderUploadError):
                            folder_upload.upload_folder(
                                self.folder, self.config, dry_run=dry_run, log_file=self.log
                            )
                    tool.assert_not_called()
                self.assertFalse(self.log.exists())
                self.assertEqual(
                    before, {str(p): p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
                )
                bad.unlink()
                Path(str(bad) + ".JSON").unlink(missing_ok=True)

    def test_folder_upload_zero_exit_error_or_invalid_log_is_failure_in_both_modes(self):
        for dry_run in (False, True):
            for text, expected in (
                ('{"level":"ERROR","msg":"walk failed"}\n', 1),
                ("not JSON", 1),
                ('{"level":[]}', 1),
                ('{"level":null}', 1),
                ("{}", 1),
                ('\n{"level":"WARN"}\n', 0),
            ):
                with self.subTest(dry_run=dry_run, text=text):
                    private = []

                    def tool(command, env, *, dry_run, private=private, text=text):
                        private.append(Path(command[command.index("--config") + 1]))
                        self.log.write_text(text)
                        return subprocess.CompletedProcess(command, 0)

                    with (
                        patch.object(folder_upload.shutil, "which", return_value="immich-go"),
                        patch.object(folder_upload, "run_upload", side_effect=tool),
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()) as error,
                    ):
                        result = folder_upload.upload_folder(
                            self.folder, self.config, dry_run=dry_run, log_file=self.log
                        )
                    self.assertEqual(result, expected)
                    self.assertFalse(private[0].exists())
                    if expected:
                        self.assertIn("recorded errors", error.getvalue())
                    self.log.unlink()

    def test_incompatible_outside_date_sidecars_do_not_obscure_known_source_dates(self):
        self.entries.update(
            {
                "Photos from 2022/old.jpg": b"old",
                "Photos from 2022/old.jpg.json": sidecar("old.jpg", "old", "1640995200"),
            }
        )
        self.write()
        self.local("old.jpg", b"old", None)
        report = self.report()
        finding = report["unexpected_local_files"][0]
        self.assertEqual(finding["classification"], "outside_date")
        self.assertEqual(finding["sidecar"], "missing")
        self.assertFalse(report["metadata_and_albums_validated"])

    def test_sidecar_symlinks_are_not_followed(self):
        sidecar_path = Path(str(self.media) + ".JSON")
        sidecar_path.unlink()
        outside = self.root / "private.json"
        outside.write_text(json.dumps(native()))
        sidecar_path.symlink_to(outside)
        self.assertEqual(sidecar_state(self.media, {}), "symlink")
        with patch.object(folder_upload, "run_upload") as tool:
            with self.assertRaises(folder_upload.FolderUploadError):
                folder_upload.upload_folder(self.folder, self.config, log_file=self.log)
            tool.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_native_reader_retains_parsed_fields_for_comparison(self):
        metadata = native(description="Retain for source comparison")
        Path(str(self.media) + ".JSON").write_text(json.dumps(metadata))
        values = {}
        self.assertEqual(sidecar_state(self.media, {}, values), "present_unverified")
        self.assertEqual(values, {self.media: metadata})
        self.assertEqual(native_sidecar_issues(Path("/X/photo(1).jpg"), metadata), [])
        self.assertEqual(native_sidecar_issues(self.media, None), ["no usable native JSON sidecar"])

    def test_native_reader_bounds_json_reads_and_accepts_the_exact_limit(self):
        real_open = Path.open

        @contextmanager
        def bounded(path, *args, **kwargs):
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
                base = json.dumps(native(padding="")).encode()
                payload = base[:-2] + b"x" * (size - len(base)) + b'"}'
                Path(str(self.media) + ".JSON").write_bytes(payload)
                with patch.object(Path, "open", autospec=True, side_effect=bounded):
                    state = sidecar_state(self.media, {})
                self.assertEqual(state, "present_unverified" if size == JSON_LIMIT else "invalid")

    def test_native_reader_does_not_accept_invalid_or_nonobject_json(self):
        for payload in (b"{", b"[]", b"null", b"\xff"):
            with self.subTest(payload=payload):
                Path(str(self.media) + ".JSON").write_bytes(payload)
                values = {}
                self.assertEqual(sidecar_state(self.media, {}, values), "invalid")
                self.assertEqual(values, {})

    def test_nonzero_tool_status_is_preserved_even_with_a_malformed_log(self):
        def tool(command, env, *, dry_run):
            self.log.write_text("bad log")
            return subprocess.CompletedProcess(command, 9)

        with (
            patch.object(folder_upload.shutil, "which", return_value="immich-go"),
            patch.object(folder_upload, "run_upload", side_effect=tool),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                folder_upload.upload_folder(self.folder, self.config, log_file=self.log), 9
            )

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_extraction_with_glob_name_is_not_certified_for_folder_upload(self):
        self.entries = {
            "Photos from 2023/holiday[1].jpg": b"photo",
            "Photos from 2023/holiday[1].jpg.json": sidecar("holiday[1].jpg")
            | {"description": "Keep me"},
        }
        self.write()
        output = self.root / "native"
        with (
            patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                takeout.main(
                    [
                        "extract",
                        "--date-range",
                        "2023",
                        "--output",
                        str(output),
                        "--log-file",
                        str(self.root / "extract.log"),
                        str(self.archive),
                    ]
                ),
                0,
            )
        report = reconcile_report([self.archive], [output], SELECTION)
        self.assertFalse(report["metadata_and_albums_validated"])
        self.assertEqual(report["selected"]["needs_review"], 1)
        self.assertIn("glob characters", report["files"][0]["local_matches"][0]["sidecar"])
        with patch.object(folder_upload, "run_upload") as upload:
            with self.assertRaises(folder_upload.FolderUploadError):
                folder_upload.upload_folder(output, self.config, dry_run=True, log_file=self.log)
            upload.assert_not_called()

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_folder_reader_preserves_literal_parentheses_but_ignores_globs_and_missing_markers(
        self,
    ):
        empty = self.root / "empty.json"
        empty.write_text("{}")
        for label, name, marker in (
            ("safe", "holiday(1).jpg", True),
            ("glob", "holiday[1].jpg", True),
            ("missing", "holiday.jpg", False),
        ):
            with self.subTest(label=label):
                source = self.root / label
                source.mkdir()
                media = source / name
                media.write_bytes(b"photo")
                metadata = native(description="Keep me", albums=[{"title": "Trip"}])
                if not marker:
                    del metadata["software"]
                Path(str(media) + ".JSON").write_text(json.dumps(metadata))
                output = self.root / (label + "-out")
                result = subprocess.run(
                    [
                        "immich-go",
                        "archive",
                        "from-folder",
                        "--config",
                        str(empty),
                        "--write-to-folder",
                        str(output),
                        "--log-file",
                        str(self.root / (label + ".log")),
                        "--save-config=false",
                        "--",
                        str(source),
                    ],
                    env=dict(os.environ, TZ="UTC", IMMICHGO_TEMPDIR=str(self.root / "cache")),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = [json.loads(p.read_text()) for p in output.rglob("*.JSON")]
                self.assertEqual(len(actual), 1 if label == "safe" else 0)
                self.assertEqual(sidecar_state(media, {}) == "present_unverified", label == "safe")
                if actual:
                    self.assertEqual(actual[0]["description"], "Keep me")
                    self.assertEqual(actual[0]["albums"], [{"title": "Trip"}])


class CompatibilityProperties(unittest.TestCase):
    @staticmethod
    @settings(max_examples=30, deadline=None, derandomize=True)
    @given(st.text(alphabet="abcXYZ019()[]?*\\_-", min_size=1, max_size=20), st.booleans())
    def test_path_metacharacters_and_marker_control_compatibility(name, marker):
        metadata = native() if marker else {}
        issues = native_sidecar_issues(Path("/safe") / (name + ".jpg"), metadata)
        assert bool(issues) == (not marker or any(c in name for c in "*?[]\\"))
