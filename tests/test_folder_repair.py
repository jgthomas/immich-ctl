"""Filename repair, metadata preservation and upstream folder-reader compatibility."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import folder_repair
from immich_ctl.folder_validate import Client
from immich_ctl.takeout_upload_audit import upload_audit
from tests.script_loader import load_script

script = load_script("immich-repair-filenames")


def invoke(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = script.main(list(map(str, args)))
    return code, out.getvalue(), err.getvalue()


def snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


class FolderRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "extracted"
        self.folder.mkdir()
        self.metadata = {
            "software": "immich-go version:0.32.0",
            "fileName": "photo.jpg",
            "dateTaken": "2013-09-25T18:32:07Z",
            "description": "An edited photograph: café",
            "latitude": 51.5,
            "longitude": -0.12,
            "favorited": True,
            "archived": False,
            "albums": [{"title": "Trip", "description": "Album narrative"}],
            "custom": {"nested": [False, None, 1, "unknown fields survive"]},
        }
        self.media, self.sidecar = self.add("2013/2013-09/photo~1.jpg", b"edited")

    def add(self, name, content, **fields):
        media = self.folder / name
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(content)
        sidecar = media.with_name(media.name + ".JSON")
        sidecar.write_text(json.dumps(self.metadata | fields))
        return media, sidecar

    def test_preview_and_real_repair_preserve_media_and_other_metadata_and_are_idempotent(self):
        _, matching = self.add("photo.jpg", b"original")
        matching_before = matching.read_bytes()
        self.sidecar.chmod(0o640)
        before = snapshot(self.folder)
        code, out, err = invoke(self.folder, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn('"action": "would_update"', out)
        self.assertIn('"old_fileName": "photo.jpg"', out)
        self.assertIn('"new_fileName": "photo~1.jpg"', out)
        self.assertIn("Files checked: 2; would update: 1; already matching: 1", out)
        self.assertEqual(snapshot(self.folder), before)
        code, out, err = invoke(self.folder)
        self.assertEqual(code, 0, err)
        self.assertIn('"action": "updated"', out)
        self.assertIn("Files checked: 2; updated: 1; already matching: 1", out)
        self.assertEqual(
            json.loads(self.sidecar.read_bytes()), self.metadata | {"fileName": self.media.name}
        )
        self.assertEqual(self.sidecar.stat().st_mode & 0o777, 0o640)
        self.assertEqual(matching.read_bytes(), matching_before)
        for name, content in before.items():
            if name != str(self.sidecar.relative_to(self.folder)):
                self.assertEqual((self.folder / name).read_bytes(), content)
        after = snapshot(self.folder)
        modified = self.sidecar.stat().st_mtime_ns
        code, out, err = invoke(self.folder)
        self.assertEqual(code, 0, err)
        self.assertIn("updated: 0; already matching: 2", out)
        self.assertEqual(snapshot(self.folder), after)
        self.assertEqual(self.sidecar.stat().st_mtime_ns, modified)

    def test_case_sensitive_media_names_and_case_insensitive_json_extensions(self):
        self.sidecar.rename(self.sidecar.with_suffix(".jSoN"))
        _, other = self.add("2013/2013-09/PHOTO~1.JPG", b"distinct upper-case photo")
        code, _, err = invoke(self.folder)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(other.read_text())["fileName"], "PHOTO~1.JPG")
        self.assertEqual(
            json.loads(self.sidecar.with_suffix(".jSoN").read_text())["fileName"], "photo~1.jpg"
        )

    def test_already_matching_file_does_not_skip_later_repairs(self):
        self.add("0-first.jpg", b"already correct", fileName="0-first.jpg")
        self.add("z-last.jpg", b"another edit")
        code, out, err = invoke(self.folder)
        self.assertEqual(code, 0, err)
        self.assertIn("Files checked: 3; updated: 2; already matching: 1", out)
        for name in ("0-first.jpg", "2013/2013-09/photo~1.jpg", "z-last.jpg"):
            data = json.loads((self.folder / (name + ".JSON")).read_text())
            self.assertEqual(data["fileName"], Path(name).name)

    def test_invalid_later_sidecar_prevents_all_writes_even_in_real_mode(self):
        _, later = self.add("z-later.jpg", b"later")
        for payload in (
            b"not JSON",
            b"[]",
            b'{"software":"different", "fileName":"old.jpg"}',
            b'{"software":"immich-go version:0.32.0","fileName":"old.jpg","fileName":"other.jpg"}',
            b'{"software":"immich-go version:0.32.0","fileName":"old.jpg","custom":{"x":1,"x":2}}',
            b'{"software":"immich-go version:0.32.0","fileName":"old.jpg","custom":NaN}',
        ):
            with self.subTest(payload=payload):
                later.write_bytes(payload)
                before = snapshot(self.folder)
                for extra in ((), ("--dry-run",)):
                    code, out, err = invoke(self.folder, *extra)
                    self.assertEqual(code, 1)
                    self.assertIn("Error:", err)
                    self.assertEqual(out, "")
                    self.assertEqual(snapshot(self.folder), before)

    def test_missing_invalid_filename_and_ambiguous_sidecars_are_rejected(self):
        original = self.sidecar.read_bytes()
        for name in (None, "", 123, False):
            with self.subTest(name=name):
                self.sidecar.write_text(json.dumps(self.metadata | {"fileName": name}))
                before = snapshot(self.folder)
                code, _, err = invoke(self.folder)
                self.assertEqual(code, 1)
                self.assertIn("fileName", err)
                self.assertEqual(snapshot(self.folder), before)
        self.sidecar.write_bytes(original)
        duplicate = self.sidecar.with_suffix(".json")
        duplicate.write_bytes(original)
        code, _, err = invoke(self.folder)
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", err)
        duplicate.unlink()
        self.sidecar.unlink()
        code, _, err = invoke(self.folder)
        self.assertEqual(code, 1)
        self.assertIn("missing", err)

    def test_unsafe_paths_symlinks_and_unsupported_files_stop_before_writing(self):
        for name in ("holiday[1].jpg", "note.txt"):
            with self.subTest(name=name):
                media, sidecar = self.add(name, b"unhandled")
                before = snapshot(self.folder)
                code, _, err = invoke(self.folder)
                self.assertEqual(code, 1)
                self.assertIn("Error:", err)
                self.assertEqual(snapshot(self.folder), before)
                media.unlink()
                sidecar.unlink()
        outside = self.root / "outside.JSON"
        outside.write_bytes(self.sidecar.read_bytes())
        self.sidecar.unlink()
        self.sidecar.symlink_to(outside)
        before = outside.read_bytes()
        code, _, err = invoke(self.folder)
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertEqual(outside.read_bytes(), before)

    def test_atomic_write_failure_leaves_original_json_and_no_temporary_files(self):
        before = snapshot(self.folder)
        with patch.object(folder_repair.os, "replace", side_effect=OSError("disk failure")):
            code, out, err = invoke(self.folder)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("disk failure", err)
        self.assertEqual(snapshot(self.folder), before)

    def test_rewrite_cannot_exceed_the_sidecar_size_limit(self):
        before = snapshot(self.folder)
        with patch.object(folder_repair, "JSON_LIMIT", len(self.sidecar.read_bytes())):
            code, out, err = invoke(self.folder)
        self.assertEqual(code, 1)
        self.assertIn("Rewritten sidecar would exceed", err)
        self.assertEqual(out, "")
        self.assertEqual(snapshot(self.folder), before)
        _, changes = folder_repair.plan(self.folder)
        with patch.object(folder_repair, "JSON_LIMIT", len(changes[0].updated)):
            self.assertEqual(invoke(self.folder)[0], 0)
        self.assertEqual(len(self.sidecar.read_bytes()), len(changes[0].updated))

    def test_atomic_replacement_uses_a_complete_file_on_the_same_filesystem(self):
        original = self.sidecar.read_bytes()
        real_replace = os.replace
        calls = []

        def replace(source, destination):
            source, destination = Path(source), Path(destination)
            self.assertEqual(source.parent, destination.parent)
            self.assertEqual(destination, self.sidecar)
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(json.loads(source.read_bytes())["fileName"], self.media.name)
            calls.append(source)
            real_replace(source, destination)

        with patch.object(folder_repair.os, "replace", side_effect=replace):
            code, _, err = invoke(self.folder)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].exists())

    def test_temporary_file_creation_failure_does_not_change_anything(self):
        before = snapshot(self.folder)
        with patch.object(
            folder_repair.tempfile, "NamedTemporaryFile", side_effect=OSError("read-only directory")
        ):
            code, _, err = invoke(self.folder)
        self.assertEqual(code, 1)
        self.assertIn("read-only directory", err)
        self.assertEqual(snapshot(self.folder), before)

    def test_changed_sidecar_is_not_overwritten_and_interrupt_returns_130(self):
        _, changes = folder_repair.plan(self.folder)
        updated = json.dumps(self.metadata | {"description": "concurrent change"}).encode()
        self.sidecar.write_bytes(updated)
        with self.assertRaisesRegex(folder_repair.RepairError, "changed since inspection"):
            folder_repair.write_change(changes[0])
        self.assertEqual(self.sidecar.read_bytes(), updated)
        with patch.object(folder_repair, "write_change", side_effect=KeyboardInterrupt):
            self.assertEqual(invoke(self.folder)[0], 130)
        self.assertEqual(self.sidecar.read_bytes(), updated)

    def test_sidecar_replaced_with_a_symlink_is_rejected_even_with_identical_contents(self):
        _, changes = folder_repair.plan(self.folder)
        outside = self.root / "outside.JSON"
        outside.write_bytes(changes[0].original)
        self.sidecar.unlink()
        self.sidecar.symlink_to(outside)
        with self.assertRaisesRegex(folder_repair.RepairError, "changed since inspection"):
            folder_repair.write_change(changes[0])
        self.assertTrue(self.sidecar.is_symlink())
        self.assertEqual(outside.read_bytes(), changes[0].original)

    def test_invalid_or_empty_folders_fail(self):
        for folder in (self.root / "missing", self.media, self.root / "empty"):
            if folder.name == "empty":
                folder.mkdir()
            self.assertEqual(invoke(folder)[0], 1)

    def test_repair_removes_reported_alias_collision_without_server_access(self):
        self.add("2013/2013-09/photo.jpg", b"different original")
        client = Client("https://photos.example/api", "synthetic-key", 30)
        with (
            patch.object(
                client,
                "read",
                side_effect=lambda path: {"/users/me": {"id": "me"}, "/albums": []}[path],
            ),
            patch.object(client, "inventory", return_value=[]),
        ):
            before = upload_audit([], client, folders=[self.folder])
            self.assertEqual(len(before["within_input_matches"]), 1)
            self.assertTrue(before["has_warnings"])
            self.assertEqual(invoke(self.folder)[0], 0)
            after = upload_audit([], client, folders=[self.folder])
        self.assertEqual(after["input_files"], 2)
        self.assertEqual(after["within_input_matches"], [])
        self.assertFalse(after["has_warnings"])


class FolderRepairPropertyTests(unittest.TestCase):
    @staticmethod
    @settings(max_examples=25, deadline=None)
    @given(
        st.text(alphabet='abcABC012 ()~_.é"', min_size=1, max_size=25),
        st.recursive(
            st.none() | st.booleans() | st.integers() | st.text(),
            lambda child: st.lists(child, max_size=4)
            | st.dictionaries(st.text(), child, max_size=4),
            max_leaves=8,
        ),
    )
    def test_arbitrary_metadata_and_literal_names_survive_repair(stem, custom):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            media = folder / ("photo-" + stem + ".jpg")
            media.write_bytes(b"original bytes")
            sidecar = media.with_name(media.name + ".JSON")
            metadata = {
                "software": "immich-go version:0.32.0",
                "fileName": "other-" + media.name,
                "custom": custom,
            }
            sidecar.write_text(json.dumps(metadata))
            assert invoke(folder, "--dry-run")[0] == 0
            assert json.loads(sidecar.read_text()) == metadata
            assert invoke(folder)[0] == 0
            assert json.loads(sidecar.read_text()) == metadata | {"fileName": media.name}
            assert media.read_bytes() == b"original bytes"
            after = snapshot(folder)
            assert invoke(folder)[0] == 0
            assert snapshot(folder) == after


class FolderRepairIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_repaired_names_and_metadata_survive_native_folder_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "recovered"
            source.mkdir()
            metadata = {
                "software": "immich-go version:0.32.0",
                "fileName": "photo.jpg",
                "dateTaken": "2013-09-25T18:32:07Z",
                "description": "Keep this edit",
                "latitude": 51.5,
                "longitude": -0.12,
                "favorited": True,
                "archived": True,
                "albums": [{"title": "Trip", "description": "Narrative"}],
            }
            contents = {
                "photo.jpg": b"original image",
                "abcd-photo-edited(1).jpg": b"distinct edited image",
            }
            for name, content in contents.items():
                (source / name).write_bytes(content)
                (source / (name + ".JSON")).write_text(json.dumps(metadata))
            self.assertEqual(invoke(source)[0], 0)
            before = snapshot(source)
            config = root / "empty.json"
            config.write_text("{}")
            env = {k: v for k, v in os.environ.items() if not k.startswith("IMMICH_GO_")}
            env.update(TZ="UTC", IMMICHGO_TEMPDIR=str(root / "cache"))
            result = subprocess.run(
                [
                    "immich-go",
                    "archive",
                    "from-folder",
                    "--config",
                    str(config),
                    "--write-to-folder",
                    str(root / "roundtrip"),
                    "--save-config=false",
                    "--log-file",
                    str(root / "roundtrip.log"),
                    "--",
                    str(source),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            sidecars = list((root / "roundtrip").rglob("*.JSON"))
            self.assertEqual(len(sidecars), 2)
            found = {}
            for path in sidecars:
                data = json.loads(path.read_text())
                name = data["fileName"]
                found[name] = path.with_suffix("").read_bytes()
                for field in (
                    "dateTaken",
                    "description",
                    "latitude",
                    "longitude",
                    "favorited",
                    "archived",
                    "albums",
                ):
                    self.assertEqual(data[field], metadata[field])
            self.assertEqual(found, contents)
            self.assertEqual(snapshot(source), before)
