"""Folder upload safeguards, generated inputs, and offline metadata integration."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import folder_upload, upload_output
from tests.script_loader import load_script

upload_script = load_script("immich-upload-folder")


def invoke(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = upload_script.main(list(map(str, arguments)))
    return code, stdout.getvalue(), stderr.getvalue()


def snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


class FolderUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "extracted"
        (self.folder / "2023/2023-01").mkdir(parents=True)
        self.media = self.folder / "2023/2023-01/photo.jpg"
        self.media.write_bytes(b"original photo")
        self.media.with_name("photo.jpg.JSON").write_text(
            json.dumps({"fileName": "photo.jpg", "albums": [{"title": "Trip"}]})
        )
        self.config = self.root / "account.ini"
        self.key = "synthetic-secret-%-key"
        self.config.write_text(
            f"[immich]\nserver=https://photos.example/api/\napi_key={self.key}\n"
        )
        self.log = self.root / "upload.log"

    def arguments(self, *extra):
        return [self.folder, "--config", self.config, "--log-file", self.log, *extra]

    def test_upload_and_dry_run_preserve_sources_and_isolate_credentials_and_options(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                before = snapshot(self.folder)
                private_paths = []

                def run(command, private_paths=private_paths, dry_run=dry_run, **kwargs):
                    private = Path(command[command.index("--config") + 1])
                    private_paths.append(private)
                    self.assertEqual(private.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(private.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(
                        json.loads(private.read_text()),
                        {"upload": {"server": "https://photos.example", "api-key": self.key}},
                    )
                    self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(
                        command,
                        [
                            "/usr/bin/immich-go",
                            "upload",
                            "from-folder",
                            "--config",
                            str(private),
                            "--recursive=true",
                            "--ignore-sidecar-files=false",
                            "--folder-as-album=NONE",
                            "--folder-as-tags=false",
                            "--date-from-name=false",
                            "--manage-burst=NoStack",
                            "--manage-heic-jpeg=NoStack",
                            "--manage-raw-jpeg=NoStack",
                            "--overwrite=false",
                            "--pause-immich-jobs=false",
                            "--save-config=false",
                            "--api-trace=false",
                            "--skip-verify-ssl=false",
                            "--no-ui",
                            "--time-zone=UTC",
                            "--on-errors=stop",
                            "--dry-run=" + str(dry_run).lower(),
                            "--log-type=JSON",
                            "--log-level=INFO",
                            "--log-file",
                            str(self.log),
                            "--",
                            str(self.folder),
                        ],
                    )
                    self.assertEqual(kwargs["env"]["TZ"], "UTC")
                    self.assertEqual(kwargs["env"]["FOLDER_UPLOAD_TEST"], "retained")
                    self.assertFalse(any(k.startswith("IMMICH_GO_") for k in kwargs["env"]))
                    self.assertIs(kwargs["check"], False)
                    return subprocess.CompletedProcess(command, 0)

                with (
                    patch.object(folder_upload.shutil, "which", return_value="/usr/bin/immich-go"),
                    patch.object(upload_output.subprocess, "run", side_effect=run),
                    patch.dict(
                        os.environ,
                        {"IMMICH_GO_UPLOAD_OVERWRITE": "true", "FOLDER_UPLOAD_TEST": "retained"},
                    ),
                ):
                    code, stdout, stderr = invoke(
                        *self.arguments(*(["--dry-run"] if dry_run else []))
                    )
                self.assertEqual(code, 0, stderr)
                self.assertIn(str(self.log), stdout)
                self.assertEqual(stderr, "")
                self.assertNotIn(self.key, stdout)
                self.assertEqual(snapshot(self.folder), before)
                self.assertEqual(len(private_paths), 1)
                self.assertFalse(private_paths[0].exists())
                self.log.unlink()

    def test_config_and_folder_are_required(self):
        for arguments in ((self.folder,), ("--config", self.config)):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                invoke(*arguments)
            self.assertEqual(error.exception.code, 2)

    def test_direct_defaults_support_nested_log_paths_and_select_the_right_tool(self):
        log = self.root / "reports/nested/upload.log"
        with (
            patch.object(folder_upload.shutil, "which", return_value="immich-go") as which,
            patch.object(
                upload_output.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(folder_upload.upload_folder(self.folder, self.config, log_file=log), 0)
        which.assert_called_once_with("immich-go")
        self.assertIn("--dry-run=false", run.call_args.args[0])
        self.assertTrue(log.is_file())

    def test_invalid_folder_or_config_does_not_start_upload_or_create_log(self):
        empty = self.root / "empty"
        empty.mkdir()
        for source in (self.root / "missing", self.media, empty):
            with self.subTest(source=source), patch.object(upload_output.subprocess, "run") as run:
                code, _, error = invoke(source, "--config", self.config, "--log-file", self.log)
                self.assertEqual(code, 1)
                self.assertIn("non-empty folder", error)
                self.assertFalse(self.log.exists())
                run.assert_not_called()
        self.config.write_text("api_key=synthetic-secret-%-key")
        with patch.object(upload_output.subprocess, "run") as run:
            code, _, error = invoke(*self.arguments())
            self.assertEqual(code, 1)
            self.assertIn("Invalid config", error)
            self.assertNotIn(self.key, error)
            run.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_log_cannot_overwrite_inputs_or_existing_logs(self):
        original_config = self.config.read_bytes()
        before = snapshot(self.folder)
        self.log.write_text("previous log")
        alias = self.root / "source-alias"
        alias.symlink_to(self.folder, target_is_directory=True)
        for log in (self.folder / "new.log", self.media, alias / "new.log", self.config, self.log):
            with (
                self.subTest(log=log),
                patch.object(folder_upload.shutil, "which", return_value="immich-go"),
                patch.object(upload_output.subprocess, "run") as run,
            ):
                code, _, error = invoke(self.folder, "--config", self.config, "--log-file", log)
                self.assertEqual(code, 1)
                self.assertIn("Error:", error)
                if log != self.log:
                    self.assertIn("outside", error.casefold())
                run.assert_not_called()
        self.assertEqual(snapshot(self.folder), before)
        self.assertEqual(self.config.read_bytes(), original_config)
        self.assertEqual(self.log.read_text(), "previous log")

    def test_default_logs_are_distinct_and_created_outside_source(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with (
                patch.object(folder_upload.shutil, "which", return_value="immich-go"),
                patch.object(
                    upload_output.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
                ),
            ):
                for _ in range(2):
                    self.assertEqual(invoke(self.folder, "--config", self.config)[0], 0)
            logs = list((self.root / "logs").glob("folder-upload-*.log"))
            self.assertEqual(len(logs), 2)
            self.assertTrue(all(log.stat().st_mode & 0o777 == 0o600 for log in logs))
        finally:
            os.chdir(previous)

    def test_tool_failures_and_interrupts_propagate_and_clean_private_config(self):
        with patch.object(folder_upload.shutil, "which", return_value=None):
            code, _, error = invoke(*self.arguments())
            self.assertEqual(code, 1)
            self.assertIn("not found on PATH", error)
            self.assertFalse(self.log.exists())
        for outcome, expected in (
            (9, 9),
            (-15, 143),
            (OSError("cannot start tool"), 1),
            (KeyboardInterrupt(), 130),
        ):
            private_paths = []

            def run(command, private_paths=private_paths, outcome=outcome, **kwargs):
                private_paths.append(Path(command[command.index("--config") + 1]))
                if isinstance(outcome, BaseException):
                    raise outcome
                return subprocess.CompletedProcess(command, outcome)

            with (
                self.subTest(outcome=outcome),
                patch.object(folder_upload.shutil, "which", return_value="immich-go"),
                patch.object(upload_output.subprocess, "run", side_effect=run),
            ):
                code, _, error = invoke(*self.arguments())
                self.assertEqual(code, expected)
                if isinstance(outcome, int):
                    self.assertIn(str(self.log), error)
                self.assertFalse(private_paths[0].exists())
                self.log.unlink()


class FolderUploadPropertyTests(unittest.TestCase):
    @staticmethod
    @settings(max_examples=25, deadline=None, derandomize=True)
    @given(
        st.text(alphabet="abcXYZ019%$'\";é🌅 -_", min_size=1, max_size=12),
        st.text(alphabet="abcXYZ019%$'\";#[]", min_size=1, max_size=20),
        st.sampled_from(["", "/", "/api", "/api/", "/immich", "/immich/api/"]),
        st.booleans(),
    )
    def test_paths_keys_and_url_forms_are_passed_without_source_changes(
        name, suffix, url_path, dry_run
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / ("extracted-" + name)
            source.mkdir()
            (source / "photo.jpg").write_bytes(b"photo")
            (source / "photo.jpg.JSON").write_text('{"albums":[{"title":"Trip"}]}')
            before = snapshot(source)
            key = "synthetic-secret-" + suffix
            config = root / "account.ini"
            config.write_text(f"[immich]\nserver=https://photos.example{url_path}\napi_key={key}\n")
            private_paths = []

            def run(command, **kwargs):
                private = Path(command[command.index("--config") + 1])
                private_paths.append(private)
                credentials = json.loads(private.read_text())["upload"]
                assert credentials["api-key"] == key
                assert credentials["server"] == "https://photos.example" + (
                    "/immich" if "/immich" in url_path else ""
                )
                assert all(key not in argument for argument in command)
                assert command[-2:] == ["--", str(source)]
                assert "--dry-run=" + str(dry_run).lower() in command
                return subprocess.CompletedProcess(command, 0)

            with (
                patch.object(folder_upload.shutil, "which", return_value="immich-go"),
                patch.object(upload_output.subprocess, "run", side_effect=run),
                redirect_stdout(io.StringIO()),
            ):
                assert (
                    folder_upload.upload_folder(
                        source, config, dry_run=dry_run, log_file=root / "run.log"
                    )
                    == 0
                )
            assert snapshot(source) == before
            assert len(private_paths) == 1
            assert not private_paths[0].exists()


class FolderMetadataIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_native_upload_command_accepts_wrapper_flags_without_network(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "extracted"
            source.mkdir()
            (source / "photo.jpg").write_bytes(b"photo")
            config = root / "account.ini"
            config.write_text("[immich]\nserver=https://photos.example\napi_key=synthetic-key\n")

            def help_only(command, **kwargs):
                result = real_run(
                    command[:3] + ["--help"] + command[3:],
                    env=kwargs["env"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertIn("Upload photos from a folder", result.stdout)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result

            with patch.object(upload_output.subprocess, "run", side_effect=help_only):
                self.assertEqual(
                    invoke(source, "--config", config, "--dry-run", "--log-file", root / "run.log")[
                        0
                    ],
                    0,
                )

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_extracted_metadata_survives_native_folder_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "takeout.zip"
            media = b"synthetic original media"
            sidecar = {
                "title": "photo.jpg",
                "url": "shared",
                "photoTakenTime": {"timestamp": "1672531200"},
                "description": "A description",
                "geoDataExif": {"latitude": 51.5, "longitude": -0.12},
                "favorited": True,
                "archived": True,
                "people": [{"name": "Example person"}],
            }
            with zipfile.ZipFile(source, "w") as archive:
                for album in ("Trip", "Family"):
                    prefix = f"Takeout/Google Photos/{album}/"
                    archive.writestr(prefix + "metadata.json", json.dumps({"title": album}))
                    archive.writestr(prefix + "photo.jpg", media)
                    archive.writestr(prefix + "photo.jpg.json", json.dumps(sidecar))
            before = source.read_bytes()
            extracted = root / "extracted"
            takeout = load_script("immich-takeout")
            with patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(root / "cache")}):
                self.assertEqual(
                    takeout.main(
                        [
                            "extract",
                            "--date-range",
                            "2023",
                            "--output",
                            str(extracted),
                            "--log-file",
                            str(root / "extract.log"),
                            str(source),
                        ]
                    ),
                    0,
                )
                before_folder = snapshot(extracted)
                empty_config = root / "empty.json"
                empty_config.write_text("{}")
                result = subprocess.run(
                    [
                        "immich-go",
                        "archive",
                        "from-folder",
                        "--config",
                        str(empty_config),
                        "--write-to-folder",
                        str(root / "roundtrip"),
                        "--save-config=false",
                        "--log-file",
                        str(root / "roundtrip.log"),
                        "--",
                        str(extracted),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata_files = list((root / "roundtrip").rglob("*.JSON"))
            self.assertEqual(len(metadata_files), 1)
            metadata = json.loads(metadata_files[0].read_text())
            self.assertEqual({album["title"] for album in metadata["albums"]}, {"Trip", "Family"})
            for field, expected in {
                "dateTaken": "2023-01-01T00:00:00Z",
                "latitude": 51.5,
                "longitude": -0.12,
                "description": "A description",
                "favorited": True,
                "archived": True,
                "tags": [{"value": "People/Example person"}],
            }.items():
                self.assertEqual(metadata[field], expected)
            self.assertEqual(metadata_files[0].with_suffix("").read_bytes(), media)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(snapshot(extracted), before_folder)
