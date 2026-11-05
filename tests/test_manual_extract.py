"""Exact manual selection, byte integrity and output safeguards."""

import hashlib
import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.manual_extract import extract, main, safe_path


class ManualExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "takeout.zip"
        self.output = self.root / "manual"
        self.name = "Photos from 2021/lp_image(1).MP4"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("Takeout/Google Photos/" + self.name, b"numbered video")
            archive.writestr("Takeout/Google Photos/Photos from 2021/lp_image.MP4", b"other")

    def test_cli_dry_run_and_extraction_preserve_only_explicit_member(self):
        before = self.archive.read_bytes()
        for dry_run in (True, False):
            out, err = io.StringIO(), io.StringIO()
            args = [str(self.archive), "--member", self.name, "--output", str(self.output)]
            with redirect_stdout(out), redirect_stderr(err):
                code = main(args + (["--dry-run"] if dry_run else []))
            self.assertEqual(code, 0, err.getvalue())
            if dry_run:
                self.assertFalse(self.output.exists())
                self.assertIn("integrity not checked", out.getvalue())
            else:
                self.assertEqual((self.output / self.name).read_bytes(), b"numbered video")
                self.assertEqual(len(list(self.output.rglob("*.MP4"))), 1)
                self.assertIn(hashlib.sha256(b"numbered video").hexdigest(), out.getvalue())
        self.assertEqual(self.archive.read_bytes(), before)

    def test_missing_duplicate_and_unsafe_selections_write_nothing(self):
        for members in (
            [self.name, "missing"],
            [self.name, self.name],
            [],
            ["../escape"],
            ["/abs"],
            ["a\\b"],
            ["a/./b"],
            ["a//b"],
        ):
            with self.subTest(members=members), self.assertRaises(ValueError):
                extract(self.archive, self.output, members, dry_run=False)
            self.assertFalse(self.output.exists())

    def test_existing_output_and_symlink_are_refused_without_changes(self):
        self.output.mkdir()
        (self.output / "keep").write_bytes(b"keep")
        for dry in (True, False):
            with self.assertRaises(ValueError):
                extract(self.archive, self.output, [self.name], dry_run=dry)
        self.assertEqual((self.output / "keep").read_bytes(), b"keep")
        alias = self.root / "alias"
        alias.symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            extract(self.archive, alias, [self.name], dry_run=False)
        self.assertTrue(alias.is_symlink())

    def test_ambiguous_members_symlinks_and_traversal_are_refused(self):
        cases = [
            [("Takeout/Google Photos/a", b"a"), ("Other/Google Photos/a", b"b")],
            [("Takeout/../Google Photos/a", b"bad")],
        ]
        symlink = zipfile.ZipInfo("Takeout/Google Photos/a")
        symlink.external_attr = 0o120777 << 16
        cases.append([(symlink, b"target")])
        for entries in cases:
            with zipfile.ZipFile(self.archive, "w") as archive:
                for name, payload in entries:
                    archive.writestr(name, payload)
            with self.assertRaises(ValueError):
                extract(self.archive, self.output, ["a"], dry_run=False)
            self.assertFalse(self.output.exists())

    def test_corrupt_media_removes_output_and_preserves_archive(self):
        # ZIP_STORED lets us corrupt the payload while leaving its recorded CRC intact.
        before = self.archive.read_bytes().replace(b"numbered video", b"corrupt! video")
        self.archive.write_bytes(before)
        with self.assertRaises(zipfile.BadZipFile):
            extract(self.archive, self.output, [self.name], dry_run=False)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.archive.read_bytes(), before)

    def test_multiple_members_and_json_are_selected_explicitly(self):
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("Takeout/Google Photos/album/photo.json", b'{"title":"photo"}')
        rows = extract(self.archive, self.output, [self.name, "album/photo.json"], dry_run=False)
        self.assertEqual([r["path"] for r in rows], [self.name, "album/photo.json"])
        self.assertEqual((self.output / "album/photo.json").read_bytes(), b'{"title":"photo"}')

    def test_file_parent_conflict_and_missing_parent_fail_before_writes(self):
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("Takeout/Google Photos/a", b"file")
            archive.writestr("Takeout/Google Photos/a/b", b"child")
        with self.assertRaises(ValueError):
            extract(self.archive, self.output, ["a", "a/b"], dry_run=False)
        with self.assertRaises(ValueError):
            extract(self.archive, self.root / "absent/output", [self.name], dry_run=False)
        self.assertFalse(self.output.exists())

    def test_compressed_media_round_trips_arbitrary_bytes(self):
        check_compressed_round_trip()

    def test_path_validation_rejects_normalization_and_control_hazards(self):
        for name in ("", "/absolute", "a\\b", "a\x00b", "a//b", "a/./b", "a/../b"):
            with self.subTest(name=name):
                self.assertFalse(safe_path(name))
        self.assertTrue(safe_path("Album 🌅/video(1).MP4"))

    def test_regular_unix_permissions_and_shared_nested_directories(self):
        names = ["root.MP4", "Album/nested/a.MP4", "Album/nested/b.json"]
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name in names:
                entry = zipfile.ZipInfo("Takeout/Google Photos/" + name)
                entry.create_system = 3
                entry.external_attr = 0o100640 << 16
                archive.writestr(entry, name.encode())
        report = extract(self.archive, self.output, names, dry_run=False)
        self.assertEqual([row["size"] for row in report], [len(name.encode()) for name in names])
        for name in names:
            self.assertEqual((self.output / name).read_bytes(), name.encode())

    def test_preview_reports_size_without_reading_corrupt_media(self):
        self.archive.write_bytes(
            self.archive.read_bytes().replace(b"numbered video", b"corrupt! video")
        )
        report = extract(self.archive, self.output, [self.name], dry_run=True)
        self.assertEqual(report, [{"path": self.name, "size": len(b"numbered video")}])
        self.assertFalse(self.output.exists())

    def test_large_copy_keeps_reads_bounded_and_preserves_all_chunks(self):
        payload = bytes(range(256)) * 12289  # Larger than several copy buffers.
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("Takeout/Google Photos/large.MP4", payload)
        original_read = zipfile.ZipExtFile.read

        def bounded_read(stream, size=-1):
            self.assertIsInstance(size, int, "Media must not be read into memory in one call")
            self.assertGreater(size, 0)
            self.assertLessEqual(size, 2 * 1024 * 1024)
            return original_read(stream, size)

        with patch.object(zipfile.ZipExtFile, "read", bounded_read):
            rows = extract(self.archive, self.output, ["large.MP4"], dry_run=False)
        self.assertEqual((self.output / "large.MP4").read_bytes(), payload)
        self.assertEqual(rows[0]["sha256"], hashlib.sha256(payload).hexdigest())

    def test_cli_missing_required_options_is_a_usage_error(self):
        for args in (
            [str(self.archive), "--output", str(self.output)],
            [str(self.archive), "--member", self.name],
        ):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(args)
            self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.output.exists())

    def test_cli_failure_reports_requested_member_on_stderr_without_traceback(self):
        missing = 'Album/missing\n"photo.MP4'
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(self.archive), "--member", missing, "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("missing", err.getvalue())
        self.assertIn("photo.MP4", err.getvalue())
        self.assertNotIn('missing\n"photo', err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertFalse(self.output.exists())

    def test_interrupt_after_one_copy_cleans_entire_batch_and_returns_130(self):
        second = "Photos from 2021/lp_image.MP4"
        original_open = zipfile.ZipFile.open
        before = self.archive.read_bytes()

        def interrupted_open(source, member, *args, **kwargs):
            if member.filename.endswith("/lp_image.MP4"):
                self.assertTrue((self.output / self.name).is_file())
                raise KeyboardInterrupt()
            return original_open(source, member, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "open", interrupted_open):
            code = main(
                [
                    str(self.archive),
                    "--member",
                    self.name,
                    "--member",
                    second,
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 130)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.archive.read_bytes(), before)


@given(st.binary(max_size=4096))
@settings(max_examples=20)
def check_compressed_round_trip(payload):
    # Keep generated examples independent of unittest instances: mutmut invokes
    # the test repeatedly in one process for discovery and clean-run checks.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive = root / "export.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as source:
            source.writestr("Takeout/Google Photos/Review/clip[1].MP4", payload)
        rows = extract(archive, root / "manual", ["Review/clip[1].MP4"], dry_run=False)
        assert (root / "manual/Review/clip[1].MP4").read_bytes() == payload
        assert rows[0]["sha256"] == hashlib.sha256(payload).hexdigest()
