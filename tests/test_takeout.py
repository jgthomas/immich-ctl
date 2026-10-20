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
