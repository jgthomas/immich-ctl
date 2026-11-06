"""Album creation/addition through the authenticated HTTP boundary."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import album
from immich_ctl.folder_validate import ValidationError


def identity(number):
    return str(UUID(int=number, version=4))


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status = status


class AlbumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner = identity(1)
        self.album_id = identity(2)
        self.ids = [identity(10), identity(11)]
        self.key = 'synthetic-key-"-\\'
        self.client = album.AlbumClient("https://photos.example/immich/api", self.key, 12)
        self.config = self.root / "account.ini"
        self.config.write_text(
            f"[immich]\nserver=https://photos.example/immich\napi_key={self.key}\n"
        )
        self.albums = []
        self.assets = {i: {"id": i, "ownerId": self.owner, "isTrashed": False} for i in self.ids}
        self.members = set()
        self.calls = []
        self.membership_result = None
        self.before = None

    def album_data(self, name="Review", identity_value=None):
        return {
            "id": identity_value or self.album_id,
            "albumName": name,
            "albumUsers": [{"user": {"id": self.owner}, "role": "owner"}],
        }

    def respond(self, request, *, timeout):
        self.assertEqual(request.get_header("X-api-key"), self.key)
        self.assertEqual(request.get_header("Accept").lower(), "application/json")
        self.assertEqual(request.get_header("Content-type").lower(), "application/json")
        self.assertEqual(timeout, self.client.timeout)
        self.assertTrue(request.full_url.startswith(self.client.server + "/"))
        path = urlsplit(request.full_url).path.removeprefix("/immich/api")
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, path, body))
        if self.before:
            self.before(method, path, body)
        if method == "GET":
            if path == "/users/me":
                return Response({"id": self.owner})
            if path == "/albums":
                self.assertEqual(urlsplit(request.full_url).query, "isOwned=true")
                return Response(self.albums)
            if path.startswith("/assets/"):
                return Response(self.assets[path.rsplit("/", 1)[-1]])
            if path.startswith("/albums/"):
                return Response(next(a for a in self.albums if a["id"] == path.rsplit("/", 1)[-1]))
        if method == "POST" and path == "/albums":
            self.assertEqual(set(body), {"albumName"})
            result = self.album_data(body["albumName"])
            self.albums.append(result)
            return Response(result, 201)
        if method == "PUT" and path == f"/albums/{self.album_id}/assets":
            if self.membership_result is not None:
                return Response(self.membership_result)
            result = [
                {
                    "id": i,
                    "success": i not in self.members,
                    **({"error": "duplicate"} if i in self.members else {}),
                }
                for i in body["ids"]
            ]
            self.members.update(body["ids"])
            return Response(result)
        self.fail(f"Unexpected method/path: {method} {path}")

    def manage(self, ids=None, **kwargs):
        with (
            patch.object(self.client.opener, "open", side_effect=self.respond),
            redirect_stdout(io.StringIO()),
        ):
            return album.manage(self.client, self.ids if ids is None else ids, **kwargs)

    def test_create_preflights_all_assets_and_adds_exact_deduplicated_ids(self):
        report = self.manage(self.ids + self.ids, name="Review")
        self.assertEqual(report["added"], self.ids)
        self.assertEqual(report["album_id"], self.album_id)
        self.assertTrue(report["create"])
        self.assertEqual(report["requested"], 2)
        self.assertEqual([c[0] for c in self.calls], ["GET"] * 4 + ["POST", "PUT"])
        self.assertEqual(self.calls[-1][2], {"ids": self.ids})
        self.assertFalse(any(c[0] in {"PATCH", "DELETE"} for c in self.calls))

    def test_repeat_run_reuses_album_and_accepts_duplicate_memberships(self):
        self.manage(name="Review")
        self.calls.clear()
        report = self.manage(name="Review")
        self.assertFalse(report["create"])
        self.assertEqual(report["added"], [])
        self.assertEqual(report["already_present"], self.ids)
        self.assertFalse(any(c[0] == "POST" for c in self.calls))

    def test_dry_runs_create_nothing_for_new_or_existing_albums(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.albums = [self.album_data()] if existing else []
                self.calls.clear()
                report = self.manage(name="Review", dry_run=True)
                self.assertEqual(report["asset_ids"], self.ids)
                self.assertEqual(report["create"], not existing)
                self.assertEqual(report["added"], [])
                self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_create_empty_album_and_select_existing_by_id(self):
        report = self.manage([], name="Review")
        self.assertEqual(report["requested"], 0)
        self.assertEqual(len(self.albums), 1)
        self.assertFalse(any(c[0] == "PUT" for c in self.calls))
        self.calls.clear()
        report = self.manage(identity=self.album_id)
        self.assertEqual(report["album_id"], self.album_id)
        self.assertEqual(self.calls[1][1], "/albums/" + self.album_id)
        self.assertFalse(any(c[0] == "POST" for c in self.calls))

    def test_duplicate_titles_and_other_owner_album_refused_before_writes(self):
        self.albums = [self.album_data(), self.album_data(identity_value=identity(3))]
        with self.assertRaisesRegex(ValidationError, "Multiple"):
            self.manage(name="Review")
        self.albums = [
            dict(self.album_data(), albumUsers=[{"user": {"id": identity(99)}, "role": "owner"}])
        ]
        with self.assertRaisesRegex(ValidationError, "ownership"):
            self.manage(identity=self.album_id)
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_wrong_or_trashed_asset_stops_before_creating_album(self):
        original = dict(self.assets[self.ids[-1]])
        for changes in (
            {"id": identity(99)},
            {"ownerId": identity(99)},
            {"isTrashed": True},
            {"isTrashed": None},
        ):
            with self.subTest(changes=changes):
                self.assets[self.ids[-1]] = dict(original, **changes)
                with self.assertRaises(ValidationError):
                    self.manage(name="Review")
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_invalid_ids_names_and_target_choice_make_no_requests(self):
        for ids, kwargs in [
            (["not-an-id"], {"name": "Review"}),
            ([], {"name": " "}),
            ([], {}),
            ([], {"name": "Review", "identity": self.album_id}),
            ([], {"identity": "../other"}),
        ]:
            with self.subTest(ids=ids, kwargs=kwargs), self.assertRaises(ValueError):
                self.manage(ids, **kwargs)
        self.assertEqual(self.calls, [])

    def test_id_files_are_bounded_validated_and_deduplicated(self):
        p = self.root / "ids.txt"
        p.write_text(self.ids[0].upper() + "\n\n" + self.ids[1] + "\t")
        self.assertEqual(album.collect_ids([self.ids[0]], [p]), self.ids)
        p.write_bytes(b"x" * (album.IDS_FILE_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            album.collect_ids([], [p])

    def test_partial_membership_failure_stops_remaining_batches(self):
        selected = [identity(20), identity(21), identity(22)]
        self.assets.update(
            {i: {"id": i, "ownerId": self.owner, "isTrashed": False} for i in selected}
        )
        self.membership_result = [
            {"id": selected[0], "success": True},
            {"id": selected[1], "success": False, "error": "no_permission"},
        ]
        with patch.object(album, "BATCH_SIZE", 2):
            report = self.manage(selected, name="Review")
        self.assertEqual(report["added"], selected[:1])
        self.assertEqual(report["failed"], selected[1:2])
        self.assertEqual(report["not_attempted"], selected[2:])
        self.assertEqual(sum(c[0] == "PUT" for c in self.calls), 1)

    def test_incomplete_unexpected_or_invalid_response_never_reports_success(self):
        for payload in (
            [],
            {},
            123,
            [{"id": self.ids[0], "success": True}],
            [{"id": self.ids[0], "success": True}] * 2,
            [{"id": i, "success": 1} for i in self.ids],
            [{"id": [], "success": True}],
            [{"id": [], "success": True}, {"id": self.ids[1], "success": True}],
            ["not a result object", {"id": self.ids[1], "success": True}],
        ):
            with self.subTest(payload=payload):
                self.membership_result = payload
                with self.assertRaisesRegex(ValidationError, "Album ID"):
                    self.manage(name="Review")

    def test_network_error_does_not_retry_or_hide_created_album_identity(self):
        def fail(method, path, body):
            if method == "PUT":
                raise URLError("private diagnostic " + self.key)

        self.before = fail
        with self.assertRaises(ValidationError) as error:
            self.manage(name="Review")
        self.assertIn(self.album_id, str(error.exception))
        self.assertIn("outcome is unknown", str(error.exception))
        self.assertNotIn(self.key, str(error.exception))
        self.assertEqual(sum(c[0] == "PUT" for c in self.calls), 1)
        self.assertEqual(len(self.albums), 1)

    def test_http_errors_and_unreadable_mutation_responses(self):
        for status in (302, 401, 403, 500):
            error = HTTPError(
                self.client.server, status, "secret " + self.key, {}, io.BytesIO(self.key.encode())
            )
            with patch.object(self.client.opener, "open", side_effect=error):
                with self.assertRaises(ValidationError) as caught:
                    self.client.request("POST", "/albums", {"albumName": "Review"})
                self.assertNotIn(self.key, str(caught.exception))
                if status >= 400:
                    self.assertIn(str(status), str(caught.exception))
        for payload, expected in ((b"bad JSON", "Invalid album JSON"), (b"x" * 33, "size limit")):
            response = Response(None)
            response = io.BytesIO(payload)
            response.status = 201
            with (
                patch.object(self.client.opener, "open", return_value=response),
                patch.object(album, "RESPONSE_LIMIT", 32),
            ):
                with self.assertRaisesRegex(ValidationError, expected):
                    self.client.request("POST", "/albums", {})

    def test_cli_prints_album_and_returns_nonzero_for_partial_failures(self):
        self.membership_result = [
            {"id": i, "success": False, "error": "not_found"} for i in self.ids
        ]
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(album, "AlbumClient", return_value=self.client),
            patch.object(self.client.opener, "open", side_effect=self.respond),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = album.main(["--name", "Review", "--config", str(self.config), *self.ids])
        self.assertEqual(code, 2)
        self.assertIn(self.album_id, out.getvalue())
        self.assertIn('"failed"', out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_cli_redacts_key_and_dry_run_uses_ids_file(self):
        p = self.root / "ids.txt"
        p.write_text("\n".join(self.ids))
        out = io.StringIO()
        with (
            patch.object(album, "AlbumClient", return_value=self.client),
            patch.object(self.client.opener, "open", side_effect=self.respond),
            redirect_stdout(out),
        ):
            code = album.main(
                [
                    "--name",
                    "Review " + self.key,
                    "--config",
                    str(self.config),
                    "--ids-file",
                    str(p),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["asset_ids"], self.ids)
        self.assertNotIn(self.key, report["album_name"])
        self.assertIn("[REDACTED]", report["album_name"])
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_generated_id_lists_preserve_unique_input_order(self):
        check_id_lists()

    def test_uuid_version_and_hyphenation_are_required(self):
        for value in (
            str(UUID(int=1, version=1)),
            identity(1).replace("-", ""),
            "{" + identity(1) + "}",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                album.asset_id(value)

    def test_id_files_combine_in_order_with_exact_size_boundary_and_bounded_reads(self):
        a, b = self.root / "a.txt", self.root / "b.txt"
        a.write_text(self.ids[0] + "\n")
        b.write_text(self.ids[1] + " " + self.ids[0])
        self.assertEqual(album.collect_ids([], [a, b]), self.ids)
        original_open = Path.open
        payload = a.read_bytes()

        class Bounded(io.BytesIO):
            def read(stream, size=-1):
                self.assertIsInstance(size, int)
                self.assertGreater(size, 0)
                self.assertLessEqual(size, len(payload) + 1)
                return super().read(size)

        def bounded_open(path, *args, **kwargs):
            if path == a:
                return Bounded(payload)
            return original_open(path, *args, **kwargs)

        with (
            patch.object(album, "IDS_FILE_LIMIT", len(payload)),
            patch.object(Path, "open", bounded_open),
        ):
            self.assertEqual(album.collect_ids([], [a]), self.ids[:1])

    def test_legacy_owner_shapes_are_supported_without_accepting_shared_albums(self):
        for owner_fields in ({"ownerId": self.owner}, {"owner": {"id": self.owner}}):
            value = {"id": self.album_id, "albumName": "Review", **owner_fields}
            self.assertEqual(album.owned_album(value, self.owner), value)
        # A present ownerId cannot be overridden by conflicting fallback data.
        with self.assertRaises(ValidationError):
            album.owned_album(dict(self.album_data(), ownerId=identity(99)), self.owner)

    def test_malformed_owner_lists_and_editor_only_membership_are_rejected(self):
        shared_owned = dict(
            self.album_data(),
            albumUsers=[
                {"user": {"id": self.owner}, "role": "owner"},
                {"user": {"id": identity(99)}, "role": "editor"},
            ],
        )
        self.assertEqual(album.owned_album(shared_owned, self.owner), shared_owned)
        invalid_users = (
            None,
            42,
            [{"user": {"id": self.owner}, "role": "editor"}],
            [{"user": "invalid", "role": "owner"}],
            [None],
            [{"user": {"id": self.owner}, "role": "owner"}] * 2,
        )
        for users in invalid_users:
            with self.subTest(users=users), self.assertRaises(ValidationError):
                album.owned_album(dict(self.album_data(), albumUsers=users), self.owner)
        for value in ([], dict(self.album_data(), albumName=123)):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                album.owned_album(value, self.owner)

    def test_invalid_account_and_album_list_responses_cannot_trigger_writes(self):
        original_read = self.client.read
        cases = [("/users/me", x) for x in ({"id": 1}, {"id": ""}, [], {})]
        cases += [("/albums?isOwned=true", x) for x in ({}, [None], [{"albumName": 1}])]
        for path, payload in cases:

            def read(request_path, response_path=path, response_payload=payload):
                return (
                    response_payload
                    if request_path == response_path
                    else original_read(request_path)
                )

            with (
                self.subTest(path=path, payload=payload),
                patch.object(self.client, "read", side_effect=read),
            ):
                with self.assertRaises(ValidationError):
                    self.manage([], name="Review")
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_wrong_album_id_and_unexpected_created_title_do_not_add_assets(self):
        original_read = self.client.read

        def wrong_album(path):
            if path == "/albums/" + self.album_id:
                return self.album_data(identity_value=identity(99))
            return original_read(path)

        with patch.object(self.client, "read", side_effect=wrong_album):
            with self.assertRaisesRegex(ValidationError, "identity"):
                self.manage(identity=self.album_id)
        self.calls.clear()
        original_request = self.client.request

        def renamed_album(method, path, body):
            return (
                self.album_data("Unexpected")
                if method == "POST"
                else original_request(method, path, body)
            )

        with patch.object(self.client, "request", side_effect=renamed_album):
            with self.assertRaisesRegex(ValidationError, "name differs"):
                self.manage(name="Review")
        self.assertFalse(any(c[0] == "PUT" for c in self.calls))

    def test_multiple_successful_batches_and_report_fields(self):
        selected = [identity(n) for n in range(20, 25)]
        self.assets.update(
            {i: {"id": i, "ownerId": self.owner, "isTrashed": False} for i in selected}
        )
        self.albums = [self.album_data("Actual album title")]
        with patch.object(album, "BATCH_SIZE", 2):
            report = self.manage(selected, identity=self.album_id)
        self.assertEqual(report["album_name"], "Actual album title")
        self.assertEqual(report["added"], selected)
        self.assertEqual(report["not_attempted"], [])
        self.assertIs(report["dry_run"], False)
        self.assertEqual(
            [c[2]["ids"] for c in self.calls if c[0] == "PUT"],
            [selected[:2], selected[2:4], selected[4:]],
        )

    def test_conflicting_success_and_error_status_is_not_accepted(self):
        self.membership_result = [
            {"id": i, "success": True, "error": "no_permission"} for i in self.ids
        ]
        with self.assertRaises(ValidationError):
            self.manage(name="Review")

    def test_http_response_size_is_bounded_and_exact_limit_is_allowed(self):
        payload = b'{"id":1}'

        class Bounded(io.BytesIO):
            status = 201

            def read(stream, size=-1):
                self.assertIsInstance(size, int)
                self.assertGreater(size, 0)
                self.assertLessEqual(size, len(payload) + 1)
                return super().read(size)

        with (
            patch.object(album, "RESPONSE_LIMIT", len(payload)),
            patch.object(self.client.opener, "open", return_value=Bounded(payload)),
        ):
            self.assertEqual(self.client.request("POST", "/albums", {}), {"id": 1})

    def test_cli_wires_real_client_url_key_timeout_and_existing_album(self):
        self.albums = [self.album_data()]
        self.client.timeout = 0.5
        out = io.StringIO()
        # Exercise the actual client construction instead of replacing its factory.
        with (
            patch("immich_ctl.folder_validate.build_opener", return_value=self.client.opener),
            patch.object(self.client.opener, "open", side_effect=self.respond),
            redirect_stdout(out),
        ):
            code = album.main(
                [
                    "--album-id",
                    self.album_id,
                    "--config",
                    str(self.config),
                    "--timeout",
                    "0.5",
                    "--dry-run",
                    *self.ids,
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["album_id"], self.album_id)
        self.assertIs(report["dry_run"], True)
        self.assertEqual(report["asset_ids"], self.ids)
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_cli_required_arguments_and_invalid_timeouts_make_no_requests(self):
        arguments = [["--config", str(self.config)], ["--name", "Review"]]
        arguments += [
            ["--name", "Review", "--config", str(self.config), "--timeout", x]
            for x in ("nan", "inf", "0", "-1")
        ]
        for args in arguments:
            with (
                self.subTest(args=args),
                redirect_stderr(io.StringIO()),
                patch.object(
                    album,
                    "AlbumClient",
                    side_effect=AssertionError(
                        "Invalid arguments must not construct a network client"
                    ),
                ),
            ):
                with self.assertRaises(SystemExit) as error:
                    album.main(args)
                self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.calls, [])

    def test_cli_errors_redact_secrets_and_preserve_created_album_id(self):
        def fail(method, path, body):
            if method == "PUT":
                raise URLError("private response " + self.key)

        self.before = fail
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(album, "AlbumClient", return_value=self.client),
            patch.object(self.client.opener, "open", side_effect=self.respond),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = album.main(["--name", "Review", "--config", str(self.config), *self.ids])
        self.assertEqual(code, 1)
        self.assertIn(self.album_id, out.getvalue())
        self.assertIn(self.album_id, err.getvalue())
        self.assertNotIn(self.key, out.getvalue() + err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(album, "manage", side_effect=ValidationError("Unexpected " + self.key)),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = album.main(["--name", "Review", "--config", str(self.config)])
        self.assertEqual(code, 1)
        message = err.getvalue()
        self.assertIn("[REDACTED]", message)
        self.assertIn("Unexpected", message)
        self.assertNotIn(json.dumps(self.key)[1:-1], message)
        self.assertEqual(out.getvalue(), "")

    def test_cli_config_failure_and_interrupt(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = album.main(["--name", "Review", "--config", str(self.root / "missing.ini")])
        self.assertEqual(code, 1)
        self.assertIn("config", err.getvalue().lower())
        self.assertEqual(out.getvalue(), "")
        with patch.object(album, "manage", side_effect=KeyboardInterrupt):
            self.assertEqual(album.main(["--name", "Review", "--config", str(self.config)]), 130)


@given(st.lists(st.uuids(version=4), max_size=30))
@settings(max_examples=30)
def check_id_lists(values):
    text = [str(v) for v in values]
    assert album.collect_ids(text + text, []) == list(dict.fromkeys(text))
