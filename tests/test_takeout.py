"""Date selection, safe orchestration and synthetic immich-go integration."""

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests.script_loader import load_script

takeout = load_script("immich-takeout")


class TakeoutCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "takeout.zip"
        self.config = self.root / "account.ini"
        self.key = "synthetic-secret-%-key"
        self.config.write_text(f"[immich]\nserver=https://photos.example\napi_key={self.key}\n")
        self.write_export(self.archive)

    def write_export(self, path, extra=None):
        entries = {}
        for title, timestamp in (
            ("first.jpg", "1672531200"),  # 2023-01-01 00:00:00 UTC
            ("last.jpg", "1704067199"),  # 2023-12-31 23:59:59 UTC
            ("next.jpg", "1704067200"),
            ("unknown.jpg", "0"),
        ):
            folder = "Takeout/Google Photos/Photos from 2023/"
            entries[folder + title] = b"synthetic media"
            entries[folder + title + ".json"] = {
                "title": title,
                "url": title,
                "photoTakenTime": {"timestamp": timestamp},
            }
        if extra:
            entries.update(extra)
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data if isinstance(data, bytes) else json.dumps(data))

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = takeout.main(list(arguments))
        self.assertNotIn(self.key, stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_date_forms_and_invalid_ranges(self):
        for value, expected in (
            ("2023", (date(2023, 1, 1), date(2023, 12, 31))),
            ("2024-02", (date(2024, 2, 1), date(2024, 2, 29))),
            ("2023-12-31", (date(2023, 12, 31), date(2023, 12, 31))),
            ("2020,2022", (date(2020, 1, 1), date(2022, 12, 31))),
            ("2023-12-31,2024-01-01", (date(2023, 12, 31), date(2024, 1, 1))),
        ):
            self.assertEqual(takeout.date_selection(value), expected)
        for value in ("", "0", "0000", "2023-02-29", "2023-13", "2024,2023", "2023,", "a,b,c"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                takeout.date_selection(value)

    def test_query_boundaries_and_no_writes(self):
        before = self.archive.read_bytes()
        with patch.object(takeout.subprocess, "run") as run:
            code, output, _ = self.invoke(
                "query", "--date-range", "2023", "--list", str(self.archive)
            )
        self.assertEqual(code, 0)
        self.assertIn("Selected metadata items: 2 of 4", output)
        self.assertIn("1 unknown dates", output)
        self.assertIn("last.jpg.json", output)
        self.assertNotIn("next.jpg.json", output)
        run.assert_not_called()
        self.assertEqual(before, self.archive.read_bytes())
        self.assertEqual({p.name for p in self.root.iterdir()}, {"takeout.zip", "account.ini"})

    def test_split_parts_reconcile_ids_and_conflicting_times(self):
        second = self.root / "second.zip"
        with zipfile.ZipFile(second, "w") as archive:
            for name, timestamp in (("first.jpg", "1672531200"), ("last.jpg", "1704067198")):
                archive.writestr(
                    "Takeout/Google Photos/Album/" + name + ".json",
                    json.dumps({"url": name, "photoTakenTime": {"timestamp": timestamp}}),
                )
            archive.writestr("Takeout/Google Photos/Album/metadata.json", '{"title":"Trip"}')
        for paths in ((self.archive, second), (second, self.archive)):
            code, output, _ = self.invoke(
                "query", "--date-range", "2023", *(str(path) for path in paths)
            )
            self.assertEqual(code, 0)
            self.assertIn("Selected metadata items: 1 of 4", output)
            self.assertIn("1 conflicting dates", output)
            self.assertIn("Trip", output)

    def test_query_counts_metadata_items_without_claiming_to_count_edited_variants(self):
        self.write_export(
            self.archive,
            {
                "Takeout/Google Photos/Photos from 2023/first-edited.jpg": b"edited version",
            },
        )
        code, output, _ = self.invoke("query", "--date-range", "2023", str(self.archive))
        self.assertEqual(code, 0)
        self.assertIn("Selected metadata items: 2 of 4", output)
        self.assertIn("Edited media variants can share metadata", output)
        self.assertIn("| Directly matched media | 2 |", output)
        self.assertIn("| Candidate edited variants | 1 |", output)
        self.assertIn("| Total candidate media | 3 |", output)

    def test_query_counts_numbered_edits_and_excludes_unselected_or_unrelated_files(self):
        prefix = "Takeout/Google Photos/Photos from 2023/"
        metadata = {
            "title": "first.jpg",
            "url": "numbered",
            "photoTakenTime": {"timestamp": "1672531200"},
        }
        self.write_export(
            self.archive,
            {
                prefix + "first(1).jpg": b"numbered original",
                prefix + "first.jpg.supplemental-metadata(1).json": metadata,
                prefix + "first-edited.jpg": b"edit",
                prefix + "first-edited(1).jpg": b"numbered edit",
                prefix + "first(1)-edited.jpg": b"alternative numbered edit",
                prefix + "first-edited(2).jpg": b"no matching numbered original",
                prefix + "first-edited.png": b"different extension",
                prefix + "FIRST-edited.jpg": b"different case",
                prefix + "first-edited.txt": b"unsupported file",
                prefix + "next-edited.jpg": b"outside selection",
                prefix + "unknown-edited.jpg": b"unknown date",
                "Takeout/Google Photos/Other/first-edited.jpg": b"different folder",
            },
        )
        before = self.archive.read_bytes()
        code, output, _ = self.invoke("query", "--date-range", "2023", str(self.archive))
        self.assertEqual(code, 0)
        self.assertIn("| Directly matched media | 3 |", output)
        self.assertIn("| Candidate edited variants | 3 |", output)
        self.assertIn("| Total candidate media | 6 |", output)
        self.assertEqual(self.archive.read_bytes(), before)

    def test_edits_with_own_metadata_use_their_own_dates_and_count_as_direct_files(self):
        prefix = "Takeout/Google Photos/Photos from 2023/"
        self.write_export(
            self.archive,
            {
                prefix + "first-edited.jpg": b"edit with its own 2024 date",
                prefix + "first-edited.jpg.json": {
                    "title": "first-edited.jpg",
                    "url": "outside-edit",
                    "photoTakenTime": {"timestamp": "1704067200"},
                },
                prefix + "last-edited.jpg": b"edit with selected date",
                prefix + "last-edited.jpg.json": {
                    "title": "last-edited.jpg",
                    "url": "selected-edit",
                    "photoTakenTime": {"timestamp": "1672531200"},
                },
            },
        )
        code, output, _ = self.invoke("query", "--date-range", "2023", str(self.archive))
        self.assertEqual(code, 0)
        self.assertIn("| Directly matched media | 3 |", output)
        self.assertIn("| Candidate edited variants | 0 |", output)
        self.assertIn("| Total candidate media | 3 |", output)

    def test_ambiguous_originals_cannot_lend_dates_to_edits(self):
        prefix = "Takeout/Google Photos/Photos from 2023/"
        self.write_export(self.archive, {prefix + "first-edited.jpg": b"edit"})
        second = self.root / "second.zip"
        with zipfile.ZipFile(second, "w") as archive:
            archive.writestr(prefix + "first.jpg", b"different original at same path")
        for paths in ((self.archive, second), (second, self.archive)):
            code, output, _ = self.invoke("query", "--date-range", "2023", *(str(p) for p in paths))
            self.assertEqual(code, 0)
            self.assertIn("| Directly matched media | 1 |", output)
            self.assertIn("| Candidate edited variants | 0 |", output)

    def test_ambiguous_edited_metadata_cannot_inherit_the_originals_date(self):
        prefix = "Takeout/Google Photos/Photos from 2023/"
        metadata = {
            "title": "first-edited.jpg",
            "photoTakenTime": {"timestamp": "1704067200"},  # Outside the selected year.
        }
        self.write_export(
            self.archive,
            {
                prefix + "first-edited.jpg": b"ambiguous edit",
                prefix + "first-edited.jpg.json": {**metadata, "url": "edit-a"},
                prefix + "first-edited.jpg.supplemental-metadata.json": {
                    **metadata,
                    "url": "edit-b",
                },
            },
        )
        code, output, _ = self.invoke("query", "--date-range", "2023", str(self.archive))
        self.assertEqual(code, 0)
        self.assertIn("| Directly matched media | 2 |", output)
        self.assertIn("| Candidate edited variants | 0 |", output)
        self.assertIn("| Total candidate media | 2 |", output)

    def test_directly_matched_non_media_files_do_not_inflate_media_counts(self):
        prefix = "Takeout/Google Photos/Photos from 2023/"
        self.write_export(
            self.archive,
            {
                prefix + "notes.bin": b"unsupported file",
                prefix + "notes.bin.json": {
                    "title": "notes.bin",
                    "url": "notes",
                    "photoTakenTime": {"timestamp": "1672531200"},
                },
            },
        )
        code, output, _ = self.invoke("query", "--date-range", "2023", str(self.archive))
        self.assertEqual(code, 0)
        self.assertIn("Selected metadata items: 3 of 5", output)
        self.assertIn("| Directly matched media | 2 |", output)
        self.assertIn("| Total candidate media | 2 |", output)

    def test_candidate_scan_continues_after_unrelated_and_unsupported_files(self):
        class UnrelatedFirst(set):
            # A set's iteration order is unspecified. Exercise the troublesome
            # order explicitly so a premature break cannot pass by chance.
            def __iter__(self):
                return iter(sorted(super().__iter__()))

        prefix = "Takeout/Google Photos/Photos from 2023/"
        for unrelated in ("000-unrelated.jpg", "000-unsupported.bin"):
            with self.subTest(unrelated=unrelated):
                self.write_export(
                    self.archive,
                    {
                        prefix + unrelated: b"unrelated contents",
                        prefix + "first-edited.jpg": b"edited contents",
                    },
                )
                *_, coverage = takeout.inspect_archives([self.archive])
                coverage.unmatched_files = UnrelatedFirst(coverage.unmatched_files)
                direct, edited = takeout.selected_media_files(
                    coverage, {("url", "first.jpg"), ("url", "last.jpg")}
                )
                self.assertEqual(len(direct), 2)
                self.assertEqual({key[0] for key in edited}, {"Photos from 2023/first-edited.jpg"})

    def test_split_zip_candidates_are_counted_once_with_repeated_parts(self):
        prefix = "Takeout/Google Photos/Photos from 2023/"
        second = self.root / "second.zip"
        with zipfile.ZipFile(second, "w") as archive:
            archive.writestr(prefix + "first-edited.jpg", b"edited bytes")
        for paths in (
            (self.archive, second),
            (second, self.archive),
            (self.archive, second, second),
        ):
            code, output, _ = self.invoke("query", "--date-range", "2023", *(str(p) for p in paths))
            self.assertEqual(code, 0)
            self.assertIn("| Directly matched media | 2 |", output)
            self.assertIn("| Candidate edited variants | 1 |", output)
            self.assertIn("| Total candidate media | 3 |", output)

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_one_metadata_item_can_extract_original_and_edited_media(self):
        source = self.root / "variants.zip"
        prefix = "Takeout/Google Photos/Photos from 2023/"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(prefix + "photo.jpg", b"original bytes")
            archive.writestr(prefix + "photo-edited.jpg", b"edited bytes")
            archive.writestr(
                prefix + "photo.jpg.json",
                json.dumps(
                    {
                        "title": "photo.jpg",
                        "url": "one-id",
                        "photoTakenTime": {"timestamp": "1672531200"},
                    }
                ),
            )
        before = source.read_bytes()
        code, output, _ = self.invoke("query", "--date-range", "2023", str(source))
        self.assertEqual(code, 0)
        self.assertIn("Selected metadata items: 1 of 1", output)
        destination = self.root / "variants"
        with patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}):
            code, _, error = self.invoke(
                "extract",
                "--date-range",
                "2023",
                "--output",
                str(destination),
                "--log-file",
                str(self.root / "variants.log"),
                str(source),
            )
        self.assertEqual(code, 0, error)
        self.assertEqual(
            {path.read_bytes() for path in destination.rglob("*.jpg")},
            {b"original bytes", b"edited bytes"},
        )
        self.assertEqual(len(list(destination.rglob("*.JSON"))), 2)
        self.assertEqual(source.read_bytes(), before)

    def test_extraction_dry_run_never_invokes_broken_upstream_dry_run(self):
        destination = self.root / "new-output"
        log = self.root / "dry-run.log"
        with patch.object(takeout.subprocess, "run") as run:
            code, output, _ = self.invoke(
                "extract",
                "--date-range",
                "2023",
                "--dry-run",
                "--output",
                str(destination),
                "--log-file",
                str(log),
                str(self.archive),
            )
        self.assertEqual(code, 0)
        self.assertIn("no files written", output)
        run.assert_not_called()
        self.assertFalse(destination.exists())
        self.assertFalse(log.exists())

    def test_upload_credentials_private_and_options_isolated(self):
        config_path = None

        def run(command, **kwargs):
            nonlocal config_path
            config_path = Path(command[command.index("--config") + 1])
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            config = json.loads(config_path.read_text())
            self.assertEqual(config["upload"]["api-key"], self.key)
            self.assertEqual(config["upload"]["server"], "https://photos.example")
            self.assertNotIn(self.key, " ".join(command))
            for option in (
                "--dry-run=true",
                "--overwrite=false",
                "--pause-immich-jobs=false",
                "--include-partner=false",
                "--include-unmatched=false",
                "--sync-albums=true",
                "--include-trashed=false",
                "--save-config=false",
                "--api-trace=false",
            ):
                self.assertIn(option, command)
            self.assertEqual(command[command.index("--date-range") + 1], "2023-01-01,2023-12-31")
            self.assertEqual(command[-2:], ["--", str(self.archive)])
            self.assertNotIn("IMMICH_GO_UPLOAD_OVERWRITE", kwargs["env"])
            self.assertEqual(kwargs["env"]["TZ"], "UTC")
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(takeout.shutil, "which", return_value="/usr/bin/immich-go"),
            patch.object(takeout.subprocess, "run", side_effect=run),
            patch.dict(os.environ, {"IMMICH_GO_UPLOAD_OVERWRITE": "true"}),
        ):
            code, _, _ = self.invoke(
                "upload",
                "--date-range",
                "2023",
                "--config",
                str(self.config),
                "--dry-run",
                "--log-file",
                str(self.root / "upload.log"),
                str(self.archive),
            )
        self.assertEqual(code, 0)
        self.assertIsNotNone(config_path)
        self.assertFalse(config_path.exists())

    def test_upload_server_root_preserves_deployment_path(self):
        for index, (configured, expected) in enumerate(
            (
                ("https://photos.example/", "https://photos.example"),
                ("https://photos.example/api/", "https://photos.example"),
                ("https://photos.example/immich", "https://photos.example/immich"),
                ("https://photos.example/immich/api", "https://photos.example/immich"),
            )
        ):
            with self.subTest(server=configured):
                self.config.write_text(f"[immich]\nserver={configured}\napi_key={self.key}\n")

                def run(command, expected=expected, **kwargs):
                    config_path = Path(command[command.index("--config") + 1])
                    config = json.loads(config_path.read_text())
                    self.assertEqual(config["upload"]["server"], expected)
                    return subprocess.CompletedProcess(command, 0)

                with (
                    patch.object(takeout.shutil, "which", return_value="immich-go"),
                    patch.object(takeout.subprocess, "run", side_effect=run),
                ):
                    self.assertEqual(
                        self.invoke(
                            "upload",
                            "--date-range",
                            "2023",
                            "--config",
                            str(self.config),
                            "--log-file",
                            str(self.root / f"upload-{index}.log"),
                            str(self.archive),
                        )[0],
                        0,
                    )

    def test_failures_and_existing_logs_preserve_inputs(self):
        with patch.object(takeout.shutil, "which", return_value="/usr/bin/immich-go"):
            log = self.root / "run.log"
            log.write_text("previous log")
            with patch.object(takeout.subprocess, "run") as run:
                self.assertEqual(
                    self.invoke(
                        "upload",
                        "--date-range",
                        "2023",
                        "--config",
                        str(self.config),
                        "--log-file",
                        str(log),
                        str(self.archive),
                    )[0],
                    1,
                )
                run.assert_not_called()
            self.assertEqual(log.read_text(), "previous log")
            with patch.object(
                takeout.subprocess, "run", return_value=subprocess.CompletedProcess([], 9)
            ):
                self.assertEqual(
                    self.invoke(
                        "upload",
                        "--date-range",
                        "2023",
                        "--config",
                        str(self.config),
                        "--log-file",
                        str(self.root / "failed.log"),
                        str(self.archive),
                    )[0],
                    9,
                )

    def test_extraction_rejects_nonempty_destination(self):
        output = self.root / "output"
        output.mkdir()
        (output / "photo.jpg").write_bytes(b"existing")
        with (
            patch.object(takeout.shutil, "which", return_value="immich-go"),
            patch.object(takeout.subprocess, "run") as run,
        ):
            self.assertEqual(
                self.invoke(
                    "extract", "--date-range", "2023", "--output", str(output), str(self.archive)
                )[0],
                1,
            )
            run.assert_not_called()
        self.assertEqual((output / "photo.jpg").read_bytes(), b"existing")

    def test_bad_zip_and_missing_tool_are_clear_errors(self):
        self.archive.write_text("not a zip")
        self.assertIn("Error:", self.invoke("query", "--date-range", "2023", str(self.archive))[2])
        self.write_export(self.archive)
        with patch.object(takeout.shutil, "which", return_value=None):
            self.assertIn(
                "not found on PATH",
                self.invoke(
                    "extract",
                    "--date-range",
                    "2023",
                    "--output",
                    str(self.root / "output"),
                    str(self.archive),
                )[2],
            )

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_actual_extraction_uses_full_final_day_and_preserves_zip(self):
        output = self.root / "output"
        before = self.archive.read_bytes()
        with patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}):
            code, _, error = self.invoke(
                "extract",
                "--date-range",
                "2023",
                "--output",
                str(output),
                "--log-file",
                str(self.root / "extract.log"),
                str(self.archive),
            )
        self.assertEqual(code, 0, error)
        self.assertTrue((output / "2023/2023-01/first.jpg").is_file())
        self.assertTrue((output / "2023/2023-12/last.jpg").is_file())
        self.assertTrue((output / "2023/2023-12/last.jpg.JSON").is_file())
        self.assertFalse(list(output.rglob("next.jpg")))
        self.assertEqual(before, self.archive.read_bytes())


if __name__ == "__main__":
    unittest.main()
