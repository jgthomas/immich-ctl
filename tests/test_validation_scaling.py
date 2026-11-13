"""Bulk validation, bounded retries, and safe resumable local hash work."""

import base64
import hashlib
import io
import json
import os
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import folder_validate as validation
from immich_ctl.validation_state import Checkpoint, fingerprint
from tests import test_folder_validate as fixtures


class ScalingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FolderValidationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.folder = self.fixture.folder
        self.db = self.root / "state.sqlite3"
        sleeper = patch.object(validation.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def invoke(self, *args, respond=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(validation, "build_opener") as opener,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            opener.return_value.open.side_effect = respond or self.fixture.respond
            code = validation.main([str(self.folder), "--config", str(self.fixture.config), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_bulk_metadata_replaces_per_file_search_and_detail_requests(self):
        assets = [self.fixture.asset]
        for i in range(25):
            path = self.folder / f"extra-{i}.jpg"
            path.write_bytes(f"extra media {i}".encode())
            Path(str(path) + ".JSON").write_text(json.dumps(self.fixture.metadata))
            assets.append(
                dict(
                    self.fixture.asset,
                    id=f"asset-{i + 2}",
                    checksum=base64.b64encode(hashlib.sha1(path.read_bytes()).digest()).decode(),
                )
            )
        calls = []

        def respond(request, *, timeout):
            body = json.loads(request.data) if request.data else None
            path = request.full_url.split("/api", 1)[1]
            calls.append((request.get_method(), path, body))
            if path == "/users/me":
                value = {"id": "me"}
            elif path == "/search/metadata":
                self.assertNotIn("checksum", body)
                self.assertFalse(body["withDeleted"])
                value = {
                    "assets": {
                        "items": assets if body["visibility"] == "archive" else [],
                        "nextPage": None,
                    }
                }
            elif path.startswith("/albums?"):
                value = [{"albumName": x} for x in self.fixture.album_names]
            else:
                self.fail("Bulk metadata must avoid individual detail reads: " + path)
            return fixtures.Response(json.dumps(value).encode())

        code, output, progress = self.invoke(
            "--json", "--checkpoint", str(self.db), respond=respond
        )
        self.assertEqual(code, 0, progress)
        self.assertEqual(json.loads(output)["counts"]["ok"], 26)
        searches = [body for _, path, body in calls if path == "/search/metadata"]
        self.assertEqual(len(searches), 6)
        self.assertEqual(
            {(b["visibility"], b["withExif"]) for b in searches},
            {(v, e) for v in ("timeline", "archive", "hidden") for e in (False, True)},
        )
        self.assertIn("Validated 26/26", progress)
        self.assertNotIn(self.fixture.key, progress)

    def test_metadata_job_not_finished_uses_detail_fallback_instead_of_missing(self):
        original = self.fixture.respond

        def respond(request, *, timeout):
            if request.data and json.loads(request.data)["withExif"]:
                return fixtures.Response(b'{"assets":{"items":[],"nextPage":null}}')
            return original(request, timeout=timeout)

        code, output, error = self.invoke("--json", "--no-checkpoint", respond=respond)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["counts"]["ok"], 1)
        self.assertEqual(sum(path.startswith("/assets/") for _, path, _ in self.fixture.calls), 1)

    def test_fallback_metadata_shared_by_identical_local_files_but_each_sidecar_compared(self):
        copy = self.folder / "copy.jpg"
        copy.write_bytes(self.fixture.media.read_bytes())
        Path(str(copy) + ".JSON").write_text(
            json.dumps(dict(self.fixture.metadata, description="Changed"))
        )
        asset = self.fixture.asset
        with (
            patch.object(validation, "validation_inventory", return_value=([asset], {})),
            patch.object(validation.Client, "read", side_effect=[{"id": "me"}, asset]) as read,
            patch.object(validation.Client, "albums", return_value={"Trip", "Family"}),
        ):
            report = validation.validate(
                self.folder, validation.Client("https://example/api", "key", 30)
            )
        self.assertEqual(read.call_count, 2)
        self.assertEqual(report["counts"]["ok"], 1)
        self.assertEqual(report["counts"]["different"], 1)

    def test_inventory_detects_new_ids_and_changed_checksums_in_metadata_batch(self):
        client = validation.Client("https://example/api", "key", 30)
        for change in ({"id": "new"}, {"checksum": base64.b64encode(b"x" * 20).decode()}):
            with (
                self.subTest(change=change),
                patch.object(
                    client,
                    "_search",
                    side_effect=[[self.fixture.asset], [dict(self.fixture.asset, **change)]],
                ),
            ):
                with self.assertRaisesRegex(validation.ValidationError, "identity changed"):
                    validation.validation_inventory(client, "me")

    def test_repeated_page_restarts_inventory_from_scratch_and_is_bounded(self):
        client = validation.Client("https://example/api", "key", 30)
        repeated = validation.ValidationError(
            "Search repeated an asset across pages; retry validation."
        )
        with patch.object(
            client, "_search", side_effect=[repeated, [self.fixture.asset], [self.fixture.asset]]
        ) as search:
            assets, details = validation.validation_inventory(client, "me")
        self.assertEqual(assets, [self.fixture.asset])
        self.assertEqual(details, {"asset-1": self.fixture.asset})
        self.assertEqual([c.kwargs for c in search.call_args_list], [{}, {}, {"with_exif": True}])
        self.sleep.reset_mock()
        with patch.object(client, "_search", side_effect=repeated) as search:
            with self.assertRaises(validation.ValidationError):
                validation.validation_inventory(client, "me")
        self.assertEqual(search.call_count, 3)
        self.assertEqual([call.args for call in self.sleep.call_args_list], [(1,), (2,)])
        with patch.object(
            client, "_search", side_effect=validation.ValidationError("malformed response")
        ) as search:
            with self.assertRaises(validation.ValidationError):
                validation.validation_inventory(client, "me")
        self.assertEqual(search.call_count, 1)

    def test_transient_http_and_network_failures_retry_but_auth_and_bad_json_do_not(self):
        client = validation.Client("https://example/api", "key", 30)
        for code in (429, 500, 502, 503, 504):
            body = fixtures.Response(b"secret response")
            failure = HTTPError("https://example", code, "secret", {}, body)
            with (
                self.subTest(code=code),
                patch.object(
                    client.opener, "open", side_effect=[failure, fixtures.Response(b'{"id":"me"}')]
                ) as request,
            ):
                self.assertEqual(client.read("/users/me"), {"id": "me"})
                self.assertEqual(request.call_count, 2)
                self.assertTrue(body.closed)
        for failure in (URLError("secret"), TimeoutError("secret"), ConnectionResetError("secret")):
            with (
                self.subTest(failure=failure),
                patch.object(
                    client.opener, "open", side_effect=[failure, failure, fixtures.Response(b"{}")]
                ) as request,
            ):
                self.assertEqual(client.read("/users/me"), {})
                self.assertEqual(request.call_count, 3)
        for code in (301, 400, 401, 403, 404, 422):
            failure = HTTPError("https://example", code, "secret", {}, io.BytesIO())
            with (
                self.subTest(code=code),
                patch.object(client.opener, "open", side_effect=failure) as request,
            ):
                with self.assertRaises(validation.ValidationError):
                    client.read("/users/me")
                self.assertEqual(request.call_count, 1)
        with patch.object(
            client.opener, "open", return_value=fixtures.Response(b"not json")
        ) as request:
            with self.assertRaises(validation.ValidationError):
                client.read("/users/me")
            self.assertEqual(request.call_count, 1)

    def test_cli_recovers_search_failure_with_progress_and_unchanged_post_payload(self):
        original = self.fixture.respond
        requests = []
        failed = False

        def respond(request, *, timeout):
            nonlocal failed
            if request.data is not None:
                requests.append((request.get_method(), request.full_url, request.data))
                if not failed:
                    failed = True
                    raise HTTPError(
                        request.full_url,
                        502,
                        "private server body",
                        {},
                        io.BytesIO(b"private server body"),
                    )
            return original(request, timeout=timeout)

        code, output, progress = self.invoke("--json", "--no-checkpoint", respond=respond)
        self.assertEqual(code, 0, progress)
        self.assertEqual(json.loads(output)["counts"]["ok"], 1)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0][0], "POST")
        self.assertIn("retry", progress.casefold())
        self.assertNotIn("private server body", progress)
        self.assertNotIn(self.fixture.key, progress)

    def test_retry_limit_backoff_interrupts_and_read_timeout(self):
        client = validation.Client("https://example/api", "key", 0.5)
        with patch.object(client.opener, "open", side_effect=TimeoutError("secret")) as request:
            with self.assertRaisesRegex(validation.ValidationError, "Connection failed"):
                client.read("/users/me")
            self.assertEqual(request.call_count, 3)
            self.assertEqual([c.kwargs["timeout"] for c in request.call_args_list], [0.5] * 3)
            self.assertEqual([c.args for c in self.sleep.call_args_list], [(1,), (2,)])
        with patch.object(client.opener, "open", side_effect=KeyboardInterrupt) as request:
            with self.assertRaises(KeyboardInterrupt):
                client.read("/users/me")
            self.assertEqual(request.call_count, 1)

    def test_checkpoint_reuses_hashes_but_refreshes_sidecars_server_metadata_and_albums(self):
        code, output, error = self.invoke("--json", "--checkpoint", str(self.db))
        self.assertEqual(code, 0, error)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.fixture.asset["exifInfo"]["description"] = "Server changed"
        self.fixture.album_names = ["Trip"]
        self.fixture.metadata["latitude"] = 20
        self.fixture.sidecar.write_text(json.dumps(self.fixture.metadata))
        self.fixture.calls.clear()
        with patch(
            "immich_ctl.local_hashes.hash_file",
            side_effect=AssertionError("Unchanged media rehashed"),
        ):
            code, output, error = self.invoke("--json", "--checkpoint", str(self.db))
        self.assertEqual(code, 2, error)
        delta = json.loads(output)["files"][0]["differences"]
        self.assertEqual(
            set(delta),
            {"description differs", "latitude differs", "missing album membership: Family"},
        )
        self.assertEqual(sum(p == "/search/metadata" for _, p, _ in self.fixture.calls), 6)
        with closing(sqlite3.connect(self.db)) as db:
            progress = json.loads(db.execute("SELECT report FROM progress").fetchone()[0])
        self.assertTrue(progress["complete"])
        self.assertEqual(progress["counts"]["different"], 1)

    def test_failure_retains_committed_hashes_and_marks_checkpoint_incomplete(self):
        asset = self.fixture.asset
        client = validation.Client("https://example/api", "key", 30)
        with (
            patch.object(
                validation, "validation_inventory", return_value=([asset], {asset["id"]: asset})
            ),
            patch.object(client, "read", return_value={"id": "me"}),
            patch.object(client, "albums", side_effect=validation.ValidationError("failed")),
            self.assertRaises(validation.ValidationError),
        ):
            validation.validate(self.folder, client, checkpoint_path=self.db)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hashes").fetchone()[0], 1)
            self.assertFalse(
                json.loads(db.execute("SELECT report FROM progress").fetchone()[0])["complete"]
            )
        with patch(
            "immich_ctl.local_hashes.hash_file",
            side_effect=AssertionError("Rehashed"),
        ):
            code, output, error = self.invoke("--json", "--checkpoint", str(self.db))
        self.assertEqual(code, 0, error)

    def test_default_checkpoint_and_quiet_keep_json_stdout_clean(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            code, output, progress = self.invoke("--json")
        self.assertEqual(code, 0, progress)
        self.assertEqual(json.loads(output)["counts"]["ok"], 1)
        self.assertIn("Server inventory:", progress)
        self.assertEqual(
            len(list((self.root / "cache" / "immich-ctl" / "validation").glob("*.sqlite3"))), 1
        )
        self.assertTrue((self.root / "cache" / "immich-ctl" / "local-hashes.sqlite3").is_file())
        code, output, progress = self.invoke("--json", "--quiet", "--no-checkpoint")
        self.assertEqual(code, 0)
        self.assertEqual(progress, "")
        self.assertEqual(json.loads(output)["counts"]["ok"], 1)

    def test_checkpoint_cannot_overwrite_source_config_symlinks_or_unrelated_files(self):
        untouched = self.fixture.media.read_bytes()
        for path in (self.folder / "new.sqlite", self.fixture.media, self.fixture.config):
            code, output, error = self.invoke("--json", "--checkpoint", str(path))
            self.assertEqual(code, 1, error)
            self.assertEqual(output, "")
        self.assertEqual(self.fixture.media.read_bytes(), untouched)
        self.assertIn(self.fixture.key, self.fixture.config.read_text())
        self.db.write_bytes(b"unrelated existing file")
        code, _, _ = self.invoke("--checkpoint", str(self.db))
        self.assertEqual(code, 1)
        self.assertEqual(self.db.read_bytes(), b"unrelated existing file")
        link = self.root / "alias.sqlite"
        link.symlink_to(self.db)
        self.assertEqual(self.invoke("--checkpoint", str(link))[0], 1)

    def test_changed_media_invalidates_hash_even_if_size_and_mtime_restored(self):
        path = self.fixture.media
        checkpoint = Checkpoint(self.db, self.folder)
        self.addCleanup(checkpoint.close)
        original = checkpoint.checksum(path)
        info = path.stat()
        path.write_bytes(b"x" * info.st_size)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertNotEqual(checkpoint.checksum(path), original)
        self.assertEqual(checkpoint.checksum(path), hashlib.sha1(path.read_bytes()).hexdigest())

    def test_hashing_detects_media_changes_during_read_and_does_not_cache(self):
        checkpoint = Checkpoint(self.db, self.folder)
        self.addCleanup(checkpoint.close)
        before = fingerprint(self.fixture.media)
        with patch(
            "immich_ctl.validation_state.fingerprint",
            side_effect=[before, before[:-1] + [before[-1] + 1]],
        ):
            with self.assertRaisesRegex(OSError, "changed while hashing"):
                checkpoint.checksum(self.fixture.media)
        self.assertEqual(checkpoint.db.execute("SELECT count(*) FROM hashes").fetchone()[0], 0)

    def test_missing_and_ambiguous_files_have_zero_checks_and_never_fetch_details(self):
        client = validation.Client("https://example/api", "key", 30)
        asset = self.fixture.asset
        for matches, status in (([], "missing"), ([asset, dict(asset, id="second")], "ambiguous")):
            with (
                self.subTest(status=status),
                patch.object(validation, "validation_inventory", return_value=(matches, {})),
                patch.object(client, "read", return_value={"id": "me"}) as read,
                patch.object(client, "albums") as albums,
            ):
                report = validation.validate(self.folder, client)
            self.assertEqual(
                report["files"][0],
                {
                    "path": "photo.jpg",
                    "checksum": self.fixture.checksum,
                    "asset_ids": [a["id"] for a in matches],
                    "differences": [],
                    "unchecked": [],
                    "checks": 0,
                    "status": status,
                },
            )
            self.assertEqual(read.call_args_list, [unittest.mock.call("/users/me")])
            albums.assert_not_called()

    def test_fallback_asset_ids_are_url_escaped_and_absent_albums_need_no_membership_calls(self):
        client = validation.Client("https://example/api", "key", 30)
        asset = dict(self.fixture.asset, id="remote/id?part=value")
        self.fixture.metadata.pop("albums")
        self.fixture.sidecar.write_text(json.dumps(self.fixture.metadata))
        with (
            patch.object(validation, "validation_inventory", return_value=([asset], {})),
            patch.object(client, "read", side_effect=[{"id": "me"}, asset]) as read,
            patch.object(client, "albums") as albums,
        ):
            report = validation.validate(self.folder, client)
        self.assertEqual(report["counts"]["ok"], 1)
        self.assertEqual(read.call_args_list[-1].args, ("/assets/remote%2Fid%3Fpart%3Dvalue",))
        albums.assert_not_called()

    def test_uncomparable_sidecar_is_explicitly_incomplete_with_text_reasons(self):
        self.fixture.sidecar.write_text(
            json.dumps({"software": "immich-go version:0.32.0", "tags": ["example"]})
        )
        code, output, error = self.invoke("--json", "--no-checkpoint")
        self.assertEqual(code, 2, error)
        row = json.loads(output)["files"][0]
        self.assertEqual(row["checks"], 0)
        self.assertEqual(row["status"], "incomplete")
        self.assertIn("no comparable sidecar metadata", row["unchecked"])
        self.assertTrue(all(isinstance(reason, str) for reason in row["unchecked"]))

    def test_corrupt_cached_hash_is_recomputed_instead_of_trusted(self):
        state = Checkpoint(self.db, self.folder)
        self.addCleanup(state.close)
        path = self.fixture.media
        expected = state.checksum(path)
        for bad in ("a", "x" * 40, "X" * 40, "f" * 41):
            with self.subTest(cached_hash=bad):
                state.db.execute("UPDATE hashes SET sha1 = ?", (bad,))
                state.db.commit()
                self.assertEqual(state.checksum(path), expected)

    def test_checkpoint_privacy_applies_from_creation_and_hardlinks_are_rejected(self):
        private = self.root / "private" / "state.sqlite"
        real_open = os.open

        def observe(path, flags, mode=0o777):
            descriptor = real_open(path, flags, mode)
            self.assertEqual(os.fstat(descriptor).st_mode & 0o777, 0o600)
            return descriptor

        with patch("immich_ctl.validation_state.os.open", side_effect=observe):
            state = Checkpoint(private, self.folder)
        state.close()
        self.assertEqual(private.parent.stat().st_mode & 0o777, 0o700)
        alias = self.root / "hardlink.sqlite"
        os.link(private, alias)
        with self.assertRaisesRegex(OSError, "hard links"):
            Checkpoint(alias, self.folder)
        unrelated = self.root / "notes.txt"
        unrelated.write_text("keep these notes")
        unrelated.chmod(0o644)
        with self.assertRaisesRegex(OSError, "not a SQLite"):
            Checkpoint(unrelated, self.folder)
        self.assertEqual(unrelated.read_text(), "keep these notes")
        self.assertEqual(unrelated.stat().st_mode & 0o777, 0o644)

    def test_midrun_checkpoint_preserves_completed_count_and_last_result(self):
        second = self.folder / "z-last.jpg"
        second.write_bytes(b"second photo")
        Path(str(second) + ".JSON").write_text(json.dumps(self.fixture.metadata))
        asset = self.fixture.asset
        other = dict(
            asset,
            id="second",
            checksum=base64.b64encode(hashlib.sha1(second.read_bytes()).digest()).decode(),
        )
        client = validation.Client("https://example/api", "key", 30)
        with (
            patch.object(
                validation,
                "validation_inventory",
                return_value=([asset, other], {asset["id"]: asset, other["id"]: other}),
            ),
            patch.object(client, "read", return_value={"id": "me"}),
            patch.object(
                client,
                "albums",
                side_effect=[
                    {"Trip", "Family"},
                    validation.ValidationError("interrupted connection"),
                ],
            ),
            self.assertRaises(validation.ValidationError),
        ):
            validation.validate(self.folder, client, checkpoint_path=self.db)
        with closing(sqlite3.connect(self.db)) as db:
            progress = json.loads(db.execute("SELECT report FROM progress").fetchone()[0])
        self.assertEqual(
            progress,
            {
                "folder": str(self.folder),
                "owner_id": "me",
                "complete": False,
                "checked": 1,
                "total": 2,
                "last_result": {
                    "path": "photo.jpg",
                    "checksum": self.fixture.checksum,
                    "asset_ids": ["asset-1"],
                    "differences": [],
                    "unchecked": [],
                    "checks": 8,
                    "status": "ok",
                },
            },
        )

    @staticmethod
    @settings(max_examples=20, deadline=None, derandomize=True)
    @given(st.binary(min_size=0, max_size=4096))
    def test_hash_checkpoint_matches_bytes_and_survives_reopening(contents):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "media"
            folder.mkdir()
            path = folder / "test.jpg"
            path.write_bytes(contents)
            db = root / "hashes.sqlite"
            for _ in range(2):
                state = Checkpoint(db, folder)
                try:
                    assert state.checksum(path) == hashlib.sha1(contents).hexdigest()
                finally:
                    state.close()
