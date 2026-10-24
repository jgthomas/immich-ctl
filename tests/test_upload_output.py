"""Dry-run reports describe simulated actions while retaining tool diagnostics."""

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import upload_output


class UploadOutputTests(unittest.TestCase):
    def test_invalid_utf8_output_preserves_diagnostics_and_exit_status(self):
        # A real local subprocess exercises decoding from the spooled file.
        # Writing str in a mocked runner would bypass errors='replace'.
        program = (
            "import os, sys\n"
            "os.write(1, b'uploaded successfully : 1\\n')\n"
            "os.write(2, b'ERROR: invalid filename \\xff; caf\\xc3\\xa9\\n')\n"
            "sys.exit(7)\n"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result = upload_output.run_upload(
                [sys.executable, "-c", program], dict(os.environ), dry_run=True
            )
        self.assertEqual(result.returncode, 7)
        self.assertIn("would upload : 1", output.getvalue())
        self.assertIn("ERROR: invalid filename \ufffd; café", output.getvalue())

    def test_dry_run_relabels_reports_preserves_errors_and_propagates_failure(self):
        transcript = (
            "Immich read 100%, Assets found: 18, Upload errors: 0, Uploaded 16\n"
            "Asset Tracking Report:\n"
            "Total Assets: 18\n"
            "  Processed: 16\n"
            "  Discarded: 2\n"
            "Event Report:\n"
            "  uploaded successfully : 16\n"
            "  metadata updated : 18\n"
            "ERROR: request failed with HTTP 403\n"
            "file: uploaded successfully.jpg\n"
        )
        captured = []

        def run(command, **kwargs):
            self.assertEqual(command, ["immich-go", "upload", "--dry-run=true"])
            self.assertEqual(kwargs["env"], {"TZ": "UTC"})
            self.assertIs(kwargs["check"], False)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            captured.append(kwargs["stdout"])
            kwargs["stdout"].write(transcript)
            return subprocess.CompletedProcess(command, 9)

        output = io.StringIO()
        with (
            patch.object(upload_output.subprocess, "run", side_effect=run),
            redirect_stdout(output),
        ):
            result = upload_output.run_upload(
                ["immich-go", "upload", "--dry-run=true"], {"TZ": "UTC"}, dry_run=True
            )
        text = output.getvalue()
        self.assertEqual(result.returncode, 9)
        for expected in (
            "DRY RUN:",
            "Would upload 16",
            "Would process: 16",
            "Would skip: 2",
            "would upload : 16",
            "metadata processing (simulated) : 18",
            "Dry-run Asset Tracking Report:",
            "Dry-run Event Report:",
            "ERROR: request failed with HTTP 403",
            "file: uploaded successfully.jpg",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("Uploaded 16", text)
        self.assertNotIn("metadata updated : 18", text)
        self.assertTrue(captured[0].closed)

    def test_real_upload_streams_unmodified_output(self):
        command, env = ["immich-go", "upload", "--dry-run=false"], {"TZ": "UTC"}
        completed = subprocess.CompletedProcess(command, 0)
        with patch.object(upload_output.subprocess, "run", return_value=completed) as run:
            self.assertIs(upload_output.run_upload(command, env, dry_run=False), completed)
        run.assert_called_once_with(command, env=env, check=False)

    def test_interrupted_or_failed_start_closes_capture(self):
        for error in (OSError("cannot start"), KeyboardInterrupt()):
            streams = []

            def run(*args, error=error, streams=streams, **kwargs):
                streams.append(kwargs["stdout"])
                raise error

            with (
                self.subTest(error=error),
                patch.object(upload_output.subprocess, "run", side_effect=run),
                redirect_stdout(io.StringIO()),
                self.assertRaises(type(error)),
            ):
                upload_output.run_upload(["immich-go"], {}, dry_run=True)
            self.assertTrue(streams[0].closed)

    @staticmethod
    @settings(max_examples=25, deadline=None, derandomize=True)
    @given(st.integers(min_value=0, max_value=100000), st.text(alphabet=" \t", max_size=5))
    def test_counts_and_indentation_survive_relabelling(count, indentation):
        original = f"{indentation}uploaded successfully : {count}\n"
        assert upload_output.dry_run_line(original) == f"{indentation}would upload : {count}\n"
        filename = f"file: {original}"
        assert upload_output.dry_run_line(filename) == filename


if __name__ == "__main__":
    unittest.main()
