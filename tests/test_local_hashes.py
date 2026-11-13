"""Cross-command hash reuse, invalidation, privacy and safe disposable-cache failures."""

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from immich_ctl import local_hashes as hashes
from immich_ctl.folder_validate import Client, validate
from immich_ctl.takeout_recover import folder_checksums
from immich_ctl.takeout_upload_audit import folder_inputs
from immich_ctl.validation_state import Checkpoint
from tests import test_folder_validate as validation_fixture
from tests import test_takeout_reconcile as reconcile_fixture
from tests import test_upload_audit as audit_fixture


class LocalHashTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "media"
        self.folder.mkdir()
        self.media = self.folder / "photo.jpg"
        self.media.write_bytes(b"media contents")
        env = patch.dict(
            os.environ,
            {"XDG_CACHE_HOME": str(self.root / "cache"), "IMMICH_CTL_NO_HASH_CACHE": "0"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.expected = {
            name: hashlib.new(name, b"media contents").hexdigest() for name in ("sha1", "sha256")
        }

    def test_both_algorithms_survive_reopening_without_reading_media(self):
        with hashes.LocalHashes([self.folder]) as cache:
            self.assertEqual(cache.checksums(self.media), self.expected)
        with (
            hashes.LocalHashes([self.folder]) as cache,
            patch.object(hashes, "hash_file", side_effect=AssertionError("rehash")),
        ):
            self.assertEqual(cache.checksums(self.media), self.expected)
        self.assertEqual(hashes.default_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(hashes.default_path().parent.stat().st_mode & 0o777, 0o700)

    def test_edits_invalidate_even_when_size_and_mtime_are_restored(self):
        with hashes.LocalHashes([self.folder]) as cache:
            cache.checksums(self.media)
            old = self.media.stat()
            self.media.write_bytes(b"x" * old.st_size)
            os.utime(self.media, ns=(old.st_atime_ns, old.st_mtime_ns))
            actual = cache.checksums(self.media)
        self.assertNotEqual(actual, self.expected)
        self.assertEqual(
            actual,
            {
                name: hashlib.new(name, self.media.read_bytes()).hexdigest()
                for name in self.expected
            },
        )

    def test_cache_retains_multiple_files_without_eviction_or_rehashing(self):
        other = self.folder / "other.jpg"
        other.write_bytes(b"other media")
        with hashes.LocalHashes([self.folder]) as cache:
            cache.checksums(self.media)
            expected_other = cache.checksums(other)
        with (
            hashes.LocalHashes([self.folder]) as cache,
            patch.object(hashes, "hash_file", side_effect=AssertionError("lost a cached file")),
        ):
            self.assertEqual(cache.checksums(self.media), self.expected)
            self.assertEqual(cache.checksums(other), expected_other)

    def test_hashing_large_media_uses_one_bounded_stream_for_both_algorithms(self):
        contents = b"0123456789" * 250000
        self.media.write_bytes(contents)
        real_open = Path.open
        opened = []

        @contextmanager
        def bounded(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as stream:
                if path != self.media:
                    yield stream
                    return
                opened.append(path)

                class Reader:
                    def fileno(reader):
                        return stream.fileno()

                    def read(reader, size=-1):
                        self.assertIsInstance(size, int)
                        self.assertTrue(0 < size <= 2 * 1024 * 1024)
                        return stream.read(size)

                yield Reader()

        with (
            hashes.LocalHashes([self.folder], enabled=False) as cache,
            patch.object(Path, "open", autospec=True, side_effect=bounded),
        ):
            result = cache.checksums(self.media)
        self.assertEqual(opened, [self.media])
        self.assertEqual(
            result, {name: hashlib.new(name, contents).hexdigest() for name in self.expected}
        )

    def test_no_checkpoint_disables_shared_cache_even_when_it_contains_the_file(self):
        state = Checkpoint(None, self.folder)
        try:
            state.checksum(self.media)
            self.assertFalse(hashes.default_path().exists())
        finally:
            state.close()
        with hashes.LocalHashes([self.folder]) as cache:
            cache.checksums(self.media)
        state = Checkpoint(None, self.folder)
        try:
            with patch.object(hashes, "hash_file", wraps=hashes.hash_file) as read:
                self.assertEqual(state.checksum(self.media), self.expected["sha1"])
            read.assert_called_once()
        finally:
            state.close()

    def test_default_cache_location_without_xdg_setting(self):
        with patch.dict(os.environ), patch.object(Path, "home", return_value=self.root):
            os.environ.pop("XDG_CACHE_HOME", None)
            self.assertEqual(
                hashes.default_path(), self.root / ".cache" / "immich-ctl" / "local-hashes.sqlite3"
            )
            with hashes.LocalHashes([self.folder]) as cache:
                self.assertEqual(cache.checksums(self.media), self.expected)

    def test_cache_is_private_from_creation_and_valid_hardlinked_databases_are_untouched(self):
        target = hashes.default_path()
        real_chmod = Path.chmod

        def verify_private(path, *args, **kwargs):
            if path == target:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return real_chmod(path, *args, **kwargs)

        previous = os.umask(0)
        try:
            with (
                patch.object(Path, "chmod", autospec=True, side_effect=verify_private),
                hashes.LocalHashes([self.folder]) as cache,
            ):
                cache.checksums(self.media)
        finally:
            os.umask(previous)
        before = target.read_bytes()
        alias = self.root / "other.sqlite3"
        os.link(target, alias)
        with hashes.LocalHashes([self.folder]) as cache:
            self.assertIsNone(cache.db)
        self.assertEqual(target.read_bytes(), before)

    def test_replacement_and_distinct_paths_cannot_reuse_old_hashes(self):
        with hashes.LocalHashes([self.folder]) as cache:
            cache.checksums(self.media)
            replacement = self.root / "replacement.jpg"
            replacement.write_bytes(b"replacement")
            replacement.replace(self.media)
            self.assertEqual(
                cache.checksums(self.media)["sha256"], hashlib.sha256(b"replacement").hexdigest()
            )
            other = self.root / "other" / self.media.name
            other.parent.mkdir()
            other.write_bytes(b"different contents")
            self.assertEqual(
                cache.checksums(other)["sha1"], hashlib.sha1(b"different contents").hexdigest()
            )

    def test_changed_during_hashing_is_not_persisted(self):
        before = hashes.fingerprint(self.media)
        with (
            hashes.LocalHashes([self.folder]) as cache,
            patch.object(
                hashes, "fingerprint", side_effect=[before, before[:-1] + [before[-1] + 1]]
            ),
        ):
            with self.assertRaisesRegex(OSError, "changed while hashing"):
                cache.checksums(self.media)
            self.assertEqual(
                cache.db.execute("SELECT count(*) FROM local_hashes_v1").fetchone()[0], 0
            )

    def test_descriptor_replacement_is_detected_before_or_after_read(self):
        for changes in ("before", "after"):
            with (
                self.subTest(changes=changes),
                hashes.LocalHashes([self.folder], enabled=False) as cache,
            ):
                original = self.media.stat()
                substitute = self.root / "substitute.jpg"
                substitute.write_bytes(b"other bytes")
                changed = substitute.stat()
                with patch.object(
                    hashes.os,
                    "fstat",
                    side_effect=[changed] if changes == "before" else [original, changed],
                ):
                    with self.assertRaisesRegex(OSError, "changed while hashing"):
                        cache.checksums(self.media)

    def test_changed_during_cache_lookup_is_not_returned(self):
        with hashes.LocalHashes([self.folder]) as cache:
            cache.checksums(self.media)
            before = hashes.fingerprint(self.media)
            with patch.object(
                hashes, "fingerprint", side_effect=[before, before[:-1] + [before[-1] + 1]]
            ):
                with self.assertRaisesRegex(OSError, "changed while reading cached"):
                    cache.checksums(self.media)

    def test_malformed_hashes_or_fingerprints_are_recomputed(self):
        with hashes.LocalHashes([self.folder]) as cache:
            cache.checksums(self.media)
            for column, bad in (
                ("sha1", "x" * 40),
                ("sha1", "f"),
                ("sha256", "X" * 64),
                ("sha256", "a" * 65),
                ("fingerprint", "not a fingerprint"),
            ):
                with self.subTest(column=column, bad=bad):
                    cache.db.execute(f"UPDATE local_hashes_v1 SET {column} = ?", (bad,))
                    cache.db.commit()
                    with patch.object(hashes, "hash_file", wraps=hashes.hash_file) as read:
                        self.assertEqual(cache.checksums(self.media), self.expected)
                    read.assert_called_once()

    def test_unavailable_invalid_or_corrupt_cache_falls_back_without_touching_media(self):
        path = hashes.default_path()
        path.parent.mkdir(parents=True)
        for data in (b"unrelated file", b"SQLite format 3\x00broken database"):
            with self.subTest(data=data):
                path.write_bytes(data)
                with hashes.LocalHashes([self.folder]) as cache:
                    self.assertIsNone(cache.db)
                    self.assertEqual(cache.checksums(self.media), self.expected)
                self.assertEqual(path.read_bytes(), data)
        path.unlink()
        with patch.object(
            hashes.sqlite3, "connect", side_effect=sqlite3.OperationalError("locked")
        ):
            with hashes.LocalHashes([self.folder]) as cache:
                self.assertEqual(cache.checksums(self.media), self.expected)
        self.assertEqual(self.media.read_bytes(), b"media contents")

    def test_database_read_or_write_failure_falls_back_to_correct_hashes(self):
        for failure in ("SELECT", "INSERT"):
            with self.subTest(failure=failure), hashes.LocalHashes([self.folder]) as cache:
                actual = cache.db
                proxy = Mock(wraps=actual)

                def execute(sql, args=(), failure=failure, actual=actual):
                    if sql.startswith(failure):
                        raise sqlite3.OperationalError("disk failure")
                    return actual.execute(sql, args)

                proxy.execute.side_effect = execute
                cache.db = proxy
                self.assertEqual(cache.checksums(self.media), self.expected)
                self.assertIsNone(cache.db)

    def test_disable_does_not_create_a_database_or_reuse_hashes(self):
        for explicit in (False, True):
            with (
                self.subTest(explicit=explicit),
                patch.dict(os.environ, {"IMMICH_CTL_NO_HASH_CACHE": "1" if explicit else "0"}),
            ):
                with hashes.LocalHashes([self.folder], enabled=explicit) as cache:
                    self.assertIsNone(cache.db)
                    self.assertEqual(cache.checksums(self.media), self.expected)
        self.assertFalse(hashes.default_path().exists())

    @unittest.skipUnless(os.name == "posix", "Unix filenames with non-UTF-8 bytes")
    def test_non_utf8_filenames_match_uncached_folder_checksums_without_source_changes(self):
        for name, contents in (
            (b"photo-\xff.jpg", b"first photo"),
            (b"photo-\xfe.jpg", b"different photo"),
        ):
            (self.folder / os.fsdecode(name)).write_bytes(contents)
        before = {path: path.read_bytes() for path in self.folder.iterdir()}
        cached = folder_checksums(self.folder)
        self.assertTrue(hashes.default_path().exists())
        with patch.dict(os.environ, {"IMMICH_CTL_NO_HASH_CACHE": "1"}):
            uncached = folder_checksums(self.folder)
        self.assertEqual(cached, uncached)
        self.assertEqual(
            cached, {hashlib.sha256(data).hexdigest(): [path.name] for path, data in before.items()}
        )
        self.assertEqual(before, {path: path.read_bytes() for path in self.folder.iterdir()})

    def test_sqlite_path_encoding_errors_on_lookup_and_insertion_fall_back_to_hashing(self):
        for failure in ("SELECT", "INSERT"):
            with self.subTest(operation=failure), hashes.LocalHashes([self.folder]) as cache:
                actual = cache.db
                proxy = Mock(wraps=actual)

                def execute(sql, args=(), failure=failure, actual=actual):
                    if sql.startswith(failure):
                        raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")
                    return actual.execute(sql, args)

                proxy.execute.side_effect = execute
                cache.db = proxy
                self.assertEqual(cache.checksums(self.media), self.expected)
                self.assertIsNone(cache.db)

    def test_cache_cannot_live_inside_any_extracted_folder(self):
        other = self.root / "other-extracted"
        other.mkdir()
        for folder in (self.folder, other):
            with patch.object(
                hashes, "default_path", return_value=folder / "cache" / "hashes.sqlite3"
            ):
                with hashes.LocalHashes([self.folder, other]) as cache:
                    self.assertEqual(cache.checksums(self.media), self.expected)
                    self.assertIsNone(cache.db)
            self.assertFalse((folder / "cache").exists())

    def test_symlink_or_hardlinked_cache_and_nonregular_media_are_rejected(self):
        path = hashes.default_path()
        path.parent.mkdir(parents=True)
        outside = self.root / "keep"
        outside.write_bytes(b"do not change")
        path.symlink_to(outside)
        with hashes.LocalHashes([self.folder]) as cache:
            self.assertIsNone(cache.db)
        path.unlink()
        os.link(outside, path)
        with hashes.LocalHashes([self.folder]) as cache:
            self.assertIsNone(cache.db)
        self.assertEqual(outside.read_bytes(), b"do not change")
        link = self.folder / "linked.jpg"
        link.symlink_to(self.media)
        with hashes.LocalHashes([self.folder], enabled=False) as cache:
            for candidate in (link, self.folder):
                with self.subTest(candidate=candidate), self.assertRaises(OSError):
                    cache.checksums(candidate)

    def test_pipeline_reuses_hashes_between_reconciliation_audit_and_validation(self):
        fx = validation_fixture.FolderValidationTests()
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.metadata["software"] = "immich-go version:0.32.0"
        fx.sidecar.write_text(json.dumps(fx.metadata))
        for first in ("sha256", "sha1"):
            with self.subTest(first=first):
                if hashes.default_path().exists():
                    hashes.default_path().unlink()
                if first == "sha256":
                    self.assertIn(
                        hashlib.sha256(fx.media.read_bytes()).hexdigest(),
                        folder_checksums(fx.folder),
                    )
                else:
                    self.assertEqual(
                        folder_inputs([fx.folder], audit_fixture.SELECTION)[0][0]["sha1"],
                        fx.checksum,
                    )
                with patch.object(
                    hashes, "hash_file", side_effect=AssertionError("media rehashed")
                ):
                    folder_checksums(fx.folder)
                    folder_inputs([fx.folder], audit_fixture.SELECTION)
                    client = Client("https://photos.example/immich/api", fx.key, 30)
                    client.opener.open = fx.respond
                    report = validate(
                        fx.folder, client, checkpoint_path=self.root / (first + ".sqlite3")
                    )
                    self.assertEqual(report["counts"]["ok"], 1)
                self.assertTrue(fx.calls)

    def test_reconciliation_reports_are_identical_cached_or_disabled(self):
        fx = reconcile_fixture.ReconcileTests()
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.local_file(fx.extracted, "renamed.jpg", b"original")
        before = {p: p.read_bytes() for p in fx.root.rglob("*") if p.is_file()}
        expected = fx.report()
        with patch.object(hashes, "hash_file", side_effect=AssertionError("media rehashed")):
            self.assertEqual(fx.report(), expected)
        with patch.dict(os.environ, {"IMMICH_CTL_NO_HASH_CACHE": "1"}):
            self.assertEqual(fx.report(), expected)
        self.assertEqual(before, {p: p.read_bytes() for p in fx.root.rglob("*") if p.is_file()})

    def test_reconciliation_never_creates_a_cache_inside_another_supplied_folder(self):
        fx = reconcile_fixture.ReconcileTests()
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.local_file(fx.extracted, "renamed.jpg", b"original")
        fx.local_file(fx.recovery, "edited.jpg", b"edited")
        destination = fx.recovery / "cache" / "local-hashes.sqlite3"
        with patch.object(hashes, "default_path", return_value=destination):
            report = fx.report(fx.extracted, fx.recovery)
        self.assertEqual(report["selected"]["present"], 2)
        self.assertFalse(destination.parent.exists())

    def test_audit_still_refreshes_sidecars_and_server_metadata_on_cache_hits(self):
        fx = audit_fixture.UploadAuditTests()
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.asset["checksum"] = base64.b64encode(hashlib.sha1(b"photo").digest()).decode()
        fx.report(folders=[fx.folder])
        fx.asset["exifInfo"]["description"] = "server description changed"
        fx.native["description"] = "new local description"
        (fx.folder / "renamed.jpg.JSON").write_text(json.dumps(fx.native))
        fx.calls.clear()
        with patch.object(hashes, "hash_file", side_effect=AssertionError("media rehashed")):
            report = fx.report(folders=[fx.folder])
        self.assertIn("description differs", report["existing_checksum"][0]["differences"])
        self.assertTrue(any(path == "/search/metadata" for _, path, _ in fx.calls))
