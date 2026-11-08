"""Cached passes must preserve whole-export collision checks and failure handling."""

import contextlib
import io
import json
import os
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from immich_ctl import collision_cache, takeout_safety
from immich_ctl.takeout_safety import check_collisions
from tests.script_loader import load_script

command = load_script("immich-takeout")


class CollisionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a, self.b = self.root / "a.zip", self.root / "b.zip"
        self.cache = self.root / "cache"
        self.write(self.a, "A/p.jpg", b"same")
        self.write(self.b, "B/p.jpg", b"same")

    def write(self, path, name, content):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Takeout/Google Photos/" + name, content)

    def records(self):
        return list(self.cache.glob("*.json"))

    def test_cold_check_opens_each_zip_once_and_warm_check_reads_no_members(self):
        with patch.object(takeout_safety.zipfile, "ZipFile", wraps=zipfile.ZipFile) as opened:
            self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(len(self.records()), 1)
        with patch.object(
            takeout_safety.zipfile, "ZipFile", side_effect=AssertionError("ZIP read")
        ):
            self.assertTrue(check_collisions([self.b, self.a, self.b], cache_dir=self.cache))
        self.assertEqual(self.records()[0].stat().st_mode & 0o777, 0o600)

    def test_refresh_rechecks_and_no_cache_never_reuses_or_writes_records(self):
        check_collisions([self.a, self.b], cache_dir=self.cache)
        with patch.object(takeout_safety.zipfile, "ZipFile", wraps=zipfile.ZipFile) as opened:
            self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache, refresh=True))
        self.assertEqual(opened.call_count, 2)
        before = self.records()[0].read_bytes()
        with patch.object(takeout_safety.zipfile, "ZipFile", wraps=zipfile.ZipFile) as opened:
            self.assertFalse(check_collisions([self.a, self.b]))
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(self.records()[0].read_bytes(), before)

    def test_same_size_rewrite_with_restored_mtime_invalidates_pass(self):
        check_collisions([self.a, self.b], cache_dir=self.cache)
        before = self.b.stat()
        self.write(self.b, "B/p.jpg", b"oops")
        os.utime(self.b, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.b.stat().st_size, before.st_size)
        with self.assertRaisesRegex(ValueError, "basename/size collision"):
            check_collisions([self.a, self.b], cache_dir=self.cache)
        self.assertEqual(len(self.records()), 1)  # Only the old successful state.

    def test_replacement_and_added_part_cannot_reuse_a_subset_pass(self):
        check_collisions([self.a, self.b], cache_dir=self.cache)
        third = self.root / "c.zip"
        self.write(third, "C/p.jpg", b"oops")
        with self.assertRaisesRegex(ValueError, "basename/size collision"):
            check_collisions([self.a, self.b, third], cache_dir=self.cache)
        third.replace(self.b)
        with self.assertRaisesRegex(ValueError, "basename/size collision"):
            check_collisions([self.a, self.b], cache_dir=self.cache)
        self.assertEqual(len(self.records()), 1)

    def test_removed_part_and_changed_policy_force_a_fresh_check(self):
        check_collisions([self.a, self.b], cache_dir=self.cache)
        with patch.object(takeout_safety.zipfile, "ZipFile", wraps=zipfile.ZipFile) as opened:
            self.assertFalse(check_collisions([self.a], cache_dir=self.cache))
        self.assertEqual(opened.call_count, 1)
        original = collision_cache.fingerprint
        with patch.object(
            collision_cache,
            "fingerprint",
            side_effect=lambda state: original(state) | {"policy": "new"},
        ):
            self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))
        self.assertEqual(len(self.records()), 3)

    def test_malformed_incomplete_false_and_oversized_records_are_rechecked(self):
        check_collisions([self.a, self.b], cache_dir=self.cache)
        record = self.records()[0]
        valid = json.loads(record.read_text())
        for payload in (
            b"broken",
            b"[]",
            b"{}",
            b"x" * (1024 * 1024 + 1),
            json.dumps(valid | {"passed": False}).encode(),
            json.dumps(valid | {"passed": 1}).encode(),
            json.dumps(valid | {"fingerprint": {}}).encode(),
        ):
            with self.subTest(payload=payload[:40]):
                record.write_bytes(payload)
                self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))
                self.assertTrue(check_collisions([self.a, self.b], cache_dir=self.cache))

    def test_unavailable_cache_and_failed_publication_do_not_skip_checks(self):
        self.cache.write_text("not a directory")
        self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))
        self.write(self.b, "B/p.jpg", b"oops")
        with self.assertRaisesRegex(ValueError, "basename/size collision"):
            check_collisions([self.a, self.b], cache_dir=self.cache)
        self.cache.unlink()
        self.write(self.b, "B/p.jpg", b"same")
        with patch.object(Path, "replace", side_effect=OSError("read-only cache")):
            self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_failed_or_interrupted_checks_never_publish_passes(self):
        self.write(self.b, "B/p.jpg", b"oops")
        with self.assertRaises(ValueError):
            check_collisions([self.a, self.b], cache_dir=self.cache)
        self.assertEqual(self.records(), [])
        self.write(self.b, "B/p.jpg", b"same")
        with patch.object(zipfile.ZipFile, "open", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                check_collisions([self.a, self.b], cache_dir=self.cache)
        self.assertEqual(self.records(), [])

    def test_crc_failure_and_duplicate_member_names_are_not_cached_as_clean(self):
        data = self.a.read_bytes()
        self.a.write_bytes(data.replace(b"same", b"oops", 1))
        with self.assertRaises(zipfile.BadZipFile):
            check_collisions([self.a, self.b], cache_dir=self.cache)
        self.assertEqual(self.records(), [])
        self.write(self.a, "A/p.jpg", b"same")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.a, "a") as archive:
                archive.writestr("Takeout/Google Photos/A/p.jpg", b"oops")
        with self.assertRaisesRegex(ValueError, "basename/size collision"):
            check_collisions([self.a], cache_dir=self.cache)
        self.assertEqual(self.records(), [])

    def test_source_change_during_check_or_cache_lookup_is_rejected(self):
        state = collision_cache.archive_state([self.a, self.b])
        changed = [dict(row, size=row["size"] + 1) for row in state]
        with patch.object(collision_cache, "archive_state", side_effect=[state, changed]):
            with self.assertRaisesRegex(ValueError, "Source archives changed"):
                check_collisions([self.a, self.b], cache_dir=self.cache)
        self.assertEqual(self.records(), [])
        check_collisions([self.a, self.b], cache_dir=self.cache)
        with patch.object(collision_cache, "archive_state", side_effect=[state, changed]):
            with self.assertRaisesRegex(ValueError, "Source archives changed"):
                check_collisions([self.a, self.b], cache_dir=self.cache)

    def test_cache_location_honours_absolute_xdg_and_ignores_relative_values(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root)}):
            self.assertEqual(
                collision_cache.default_directory(), self.root / "immich-ctl/collisions"
            )
        with patch.dict(os.environ, {"XDG_CACHE_HOME": "relative"}):
            self.assertEqual(
                collision_cache.default_directory(), Path.home() / ".cache/immich-ctl/collisions"
            )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                collision_cache.default_directory(), Path.home() / ".cache/immich-ctl/collisions"
            )

    def test_each_file_identity_attribute_independently_invalidates_a_pass(self):
        check_collisions([self.a, self.b], cache_dir=self.cache)
        original = Path.stat
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        for field in fields:

            def modified(path, *args, changed_field=field, **kwargs):
                info = original(path, *args, **kwargs)
                if path != self.a:
                    return info
                values = {name: getattr(info, name) for name in fields}
                values[changed_field] += 1
                return SimpleNamespace(**values)

            with self.subTest(field=field), patch.object(Path, "stat", modified):
                self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))

    def test_unreadable_policy_disables_cache_and_each_policy_source_invalidates_it(self):
        check_collisions([self.a, self.b], cache_dir=self.cache)
        original = Path.read_bytes
        for source in (
            Path(collision_cache.__file__),
            Path(takeout_safety.__file__),
            Path(collision_cache.takeout.__file__),
        ):

            def changed(path, changed_source=source):
                return original(path) + (b"\n# policy changed" if path == changed_source else b"")

            with self.subTest(source=source), patch.object(Path, "read_bytes", changed):
                self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))
        count = len(self.records())
        with patch.object(collision_cache, "fingerprint", return_value=None):
            self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))
        self.assertEqual(len(self.records()), count)

    def test_only_supported_media_compete_and_late_collisions_name_both_members(self):
        prefix = b"x" * (1024 * 1024 + 10)
        with zipfile.ZipFile(self.a, "w") as archive:
            archive.writestr("Takeout/Google Photos/A/aaa.jpg", b"unique")
            archive.writestr("Takeout/Google Photos/A/z.jpg", prefix + b"a")
            archive.writestr("Takeout/Google Photos/A/info.txt", b"aaaa")
        with zipfile.ZipFile(self.b, "w") as archive:
            archive.writestr("Takeout/Other/z.jpg", prefix + b"c")
            archive.writestr("Takeout/Google Photos/B/z.jpg", prefix + b"b")
            archive.writestr("Takeout/Google Photos/B/info.txt", b"bbbb")
        with self.assertRaisesRegex(ValueError, "basename/size collision") as error:
            check_collisions([self.a, self.b], cache_dir=self.cache)
        self.assertIn('"A/z.jpg"', str(error.exception))
        self.assertIn('"B/z.jpg"', str(error.exception))
        self.write(self.b, "B/z.jpg", prefix + b"a")
        with zipfile.ZipFile(self.b, "a") as archive:
            archive.writestr("Takeout/Google Photos/B/info.txt", b"bbbb")
            archive.writestr("Takeout/Google Photos/Empty/", b"")
        self.assertFalse(check_collisions([self.a, self.b], cache_dir=self.cache))

    def test_cli_cold_warm_and_bypassed_dry_runs_have_identical_reports(self):
        with zipfile.ZipFile(self.a, "a") as archive:
            archive.writestr(
                "Takeout/Google Photos/A/p.jpg.json",
                json.dumps(
                    {"title": "p.jpg", "url": "id", "photoTakenTime": {"timestamp": "1767225600"}}
                ),
            )
        arguments = [
            "extract",
            "--date-range",
            "2026",
            "--output",
            str(self.root / "output"),
            "--dry-run",
            "--timings",
            str(self.a),
            str(self.b),
        ]
        outputs = []
        timings = []
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)}):
            for extra in ([], [], ["--no-cache"], ["--refresh-cache"]):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(command.main(arguments + extra), 0)
                outputs.append(stdout.getvalue())
                timings.append(stderr.getvalue())
        self.assertTrue(all(output == outputs[0] for output in outputs))
        self.assertIn("Selected metadata items: 1 of 1", outputs[0])
        self.assertIn("(fresh)", timings[0])
        self.assertIn("(cached)", timings[1])
        self.assertIn("(fresh)", timings[2])
        self.assertIn("(fresh)", timings[3])
        self.assertTrue(all("Metadata query:" in text for text in timings))
        self.assertFalse((self.root / "output").exists())

    def test_cache_cannot_create_files_inside_extraction_output(self):
        destination = self.root / "output"
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(destination)}):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(
                    command.main(
                        [
                            "extract",
                            "--date-range",
                            "2026",
                            "--output",
                            str(destination),
                            "--dry-run",
                            str(self.a),
                        ]
                    ),
                    1,
                )
        self.assertIn("cache must be outside", errors.getvalue())
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
