"""Local extension repair, metadata preservation, collisions and failure recovery."""

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import filetype_repair as repair
from immich_ctl import folder_validate
from immich_ctl.folder_repair import RepairError
from immich_ctl.folder_validate import inspect_folder
from immich_ctl.image_format import corrected_suffix


def snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


class FiletypeRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "extracted"
        self.folder.mkdir()
        self.manifest = self.root / "manifest.json"
        self.metadata = {
            "software": "immich-go version:0.32.0",
            "fileName": "original-alias.HEIC",
            "dateTaken": "2022-06-27T20:25:11Z",
            "latitude": 52.4,
            "longitude": -1.9,
            "description": "Keep this",
            "favorited": True,
            "archived": False,
            "albums": [{"title": "Trip", "description": "Narrative", "latitude": 51.5}],
            "unknownCustomField": {"nested": [1, 2]},
        }

    def pair(self, name="photo.HEIC", payload=b"\xff\xd8\xffEXIF orientation=6; pixels unchanged"):
        media = self.folder / name
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(payload)
        sidecar = media.with_name(media.name + ".JSON")
        sidecar.write_text(json.dumps(self.metadata))
        return media, sidecar

    def run_repair(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return repair.repair(self.folder, self.manifest, **kwargs)

    def test_dry_run_then_apply_preserves_bytes_metadata_modes_and_manifest(self):
        media, sidecar = self.pair("Album/photo.HEIC")
        media.chmod(0o640)
        sidecar.chmod(0o600)
        before = snapshot(self.folder)
        planned = self.run_repair(dry_run=True)
        self.assertEqual(snapshot(self.folder), before)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(planned["changes"][0]["new_path"], "Album/photo.jpg")
        result = self.run_repair()
        target = media.with_suffix(".jpg")
        updated = json.loads(target.with_name(target.name + ".JSON").read_text())
        self.assertEqual(updated, dict(self.metadata, fileName="photo.jpg"))
        self.assertEqual(target.read_bytes(), before["Album/photo.HEIC"])
        self.assertFalse(media.exists())
        self.assertFalse(sidecar.exists())
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(target.with_name(target.name + ".JSON").stat().st_mode & 0o777, 0o600)
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest, result)
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(manifest["folder"], str(self.folder))
        self.assertEqual(manifest["checked"], 1)
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)
        row = manifest["changes"][0]
        self.assertEqual(row["old_path"], "Album/photo.HEIC")
        self.assertEqual(row["old_sidecar"], "Album/photo.HEIC.JSON")
        self.assertEqual(row["new_sidecar"], "Album/photo.jpg.JSON")
        self.assertEqual(row["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(
            row["old_sidecar_sha256"], hashlib.sha256(before["Album/photo.HEIC.JSON"]).hexdigest()
        )
        self.assertEqual(
            row["new_sidecar_sha256"],
            hashlib.sha256(target.with_name(target.name + ".JSON").read_bytes()).hexdigest(),
        )
        after = snapshot(self.root)
        self.assertEqual(self.run_repair()["changes"], [])
        self.assertEqual(snapshot(self.root), after)

    def test_recognised_signatures_and_correct_extensions(self):
        cases = [
            ("jpeg.PNG", b"\xff\xd8\xffdata", ".jpg"),
            ("png.JPG", b"\x89PNG\r\n\x1a\ndata", ".png"),
            ("valid.JPG", b"\xff\xd8\xffdata", None),
            ("valid.JPEG", b"\xff\xd8\xffdata", None),
            ("valid.jpe", b"\xff\xd8\xffdata", None),
            ("valid.PNG", b"\x89PNG\r\n\x1a\ndata", None),
            ("real.heic", b"\x00\x00\x00\x18ftypheic", None),
            ("unknown.heic", b"other content", None),
        ]
        for name, content, expected in cases:
            with self.subTest(name=name):
                media, _ = self.pair(name, content)
                self.assertEqual(corrected_suffix(media), expected)

    def test_png_repair_and_unaffected_files_remain_untouched(self):
        self.pair("a.JPG", b"\x89PNG\r\n\x1a\nPNG contents")
        valid, sidecar = self.pair("keep.jpeg")
        before = snapshot(self.folder)
        self.run_repair()
        self.assertEqual((self.folder / "a.png").read_bytes(), before["a.JPG"])
        self.assertEqual(valid.read_bytes(), before["keep.jpeg"])
        self.assertEqual(sidecar.read_bytes(), before["keep.jpeg.JSON"])
        self.assertFalse(inspect_folder(self.folder)[1])

    def test_existing_destination_or_two_planned_names_colliding_prevents_all_writes(self):
        for name in ("a.jpg", "a.JPG", "a.PNG"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                folder = self.folder
                self.folder = Path(temporary)
                self.pair("a.HEIC")
                self.pair(name)
                before = snapshot(self.folder)
                with self.assertRaises(RepairError):
                    self.run_repair()
                self.assertEqual(snapshot(self.folder), before)
                self.assertFalse(self.manifest.exists())
                self.folder = folder

    def test_invalid_sidecars_prevent_changes_to_other_valid_pairs(self):
        for payload in (
            '{"fileName":"x"}',
            '{"software":"immich-go version:0.32.0","fileName":"x","x":1,"x":2}',
            '{"software":"immich-go version:0.32.0","fileName":"x","x":NaN}',
            "[]",
            "broken",
        ):
            with self.subTest(payload=payload):
                self.pair("a.HEIC")
                _, sidecar = self.pair("z.HEIC")
                sidecar.write_text(payload)
                before = snapshot(self.folder)
                with self.assertRaises((RepairError, ValueError)):
                    self.run_repair()
                self.assertEqual(snapshot(self.folder), before)
                self.assertFalse(self.manifest.exists())

    def test_missing_or_ambiguous_sidecars_symlinks_and_unsafe_paths(self):
        media, sidecar = self.pair()
        sidecar.unlink()
        with self.assertRaises(RepairError):
            self.run_repair()
        sidecar.write_text(json.dumps(self.metadata))
        duplicate = sidecar.with_suffix(".json")
        duplicate.write_bytes(sidecar.read_bytes())
        with self.assertRaises(RepairError):
            self.run_repair()
        duplicate.unlink()
        media.unlink()
        media.symlink_to(self.root / "outside")
        with self.assertRaises(RepairError):
            self.run_repair()
        media.unlink()
        sidecar.unlink()
        self.pair("bad[1].HEIC")
        with self.assertRaises(RepairError):
            self.run_repair()
        self.assertFalse(self.manifest.exists())

    def test_manifest_collision_and_location_guards(self):
        self.pair()
        self.manifest.write_bytes(b"existing")
        before = snapshot(self.root)
        with self.assertRaises(RepairError):
            self.run_repair()
        self.assertEqual(snapshot(self.root), before)
        for path in (self.folder / "manifest.json", self.root / "absent/manifest.json"):
            with self.subTest(path=path), self.assertRaises(RepairError):
                repair.repair(self.folder, path, dry_run=True)
        self.manifest.unlink()
        self.manifest.symlink_to(self.root / "missing")
        with self.assertRaises(RepairError):
            self.run_repair()

    def test_changed_source_is_not_renamed(self):
        media, sidecar = self.pair()
        _, changes = repair.plan(self.folder)
        for path in (sidecar, media):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                with self.assertRaises(RepairError):
                    repair.apply(changes[0])
                self.assertFalse(media.with_suffix(".jpg").exists())
                path.write_bytes(original)

    def test_pair_failure_rolls_back_but_manifest_records_incomplete_batch(self):
        media, sidecar = self.pair()
        before = snapshot(self.folder)
        unlink = Path.unlink

        def fail_original_sidecar(path, *args, **kwargs):
            if path == sidecar:
                raise OSError("simulated removal failure")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_original_sidecar), self.assertRaises(OSError):
            self.run_repair()
        self.assertEqual(snapshot(self.folder), before)
        self.assertEqual(json.loads(self.manifest.read_text())["state"], "planned")
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)
        self.assertTrue(media.exists())

    def test_racing_target_is_never_overwritten_or_removed(self):
        media, _ = self.pair()
        _, changes = repair.plan(self.folder)
        target = media.with_suffix(".jpg")
        target.write_bytes(b"other image")
        with self.assertRaises(FileExistsError):
            repair.apply(changes[0])
        self.assertEqual(target.read_bytes(), b"other image")
        self.assertTrue(media.exists())

    def test_checker_flags_mismatch_and_cli_error_is_clear(self):
        self.pair()
        self.assertTrue(any("image format mismatch" in w for w in inspect_folder(self.folder)[1]))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = repair.main([str(self.folder), "--manifest", str(self.folder / "bad.json")])
        self.assertEqual(code, 1)
        self.assertIn("outside", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_property_preserves_jpeg_bytes_and_arbitrary_metadata(self):
        check_repair_round_trip()

    def test_correctly_named_file_does_not_hide_later_mismatches(self):
        self.pair("a.jpg")
        self.pair("z.HEIC")
        report = self.run_repair()
        self.assertEqual(report["checked"], 2)
        self.assertEqual([r["old_path"] for r in report["changes"]], ["z.HEIC"])
        self.assertTrue((self.folder / "z.jpg").is_file())

    def test_native_filename_must_be_nonempty_text(self):
        _, sidecar = self.pair()
        for name in (None, "", 42, True):
            with self.subTest(name=name):
                sidecar.write_text(json.dumps(dict(self.metadata, fileName=name)))
                with self.assertRaises(RepairError):
                    self.run_repair()
        self.assertFalse(self.manifest.exists())

    def test_escaped_version_marker_is_not_upstream_compatible(self):
        _, sidecar = self.pair()
        sidecar.write_text(
            json.dumps(self.metadata).replace("immich-go version", "\\u0069mmich-go version")
        )
        with self.assertRaises(RepairError):
            self.run_repair()
        self.assertFalse(self.manifest.exists())

    def test_json_read_limits_and_rewritten_size_are_enforced(self):
        _, sidecar = self.pair()
        payload = json.dumps(self.metadata).encode()
        # Both reader paths enforce bounded reads, including inspection before repair.
        original_open = Path.open

        class Bounded(io.BytesIO):
            def read(stream, size=-1):
                self.assertIsInstance(size, int)
                self.assertGreater(size, 0)
                self.assertLessEqual(size, len(payload) + 1)
                return super().read(size)

        def bounded_open(path, *args, **kwargs):
            if path == sidecar and args == ("rb",):
                return Bounded(payload)
            return original_open(path, *args, **kwargs)

        with (
            patch.object(repair, "JSON_LIMIT", len(payload)),
            patch.object(folder_validate, "JSON_LIMIT", len(payload)),
            patch.object(Path, "open", bounded_open),
        ):
            # The compact source fits exactly; pretty-printed output does not.
            with self.assertRaisesRegex(RepairError, "Rewritten"):
                repair.plan(self.folder)
        with patch.object(repair, "JSON_LIMIT", len(payload) - 1):
            with self.assertRaisesRegex(RepairError, "Oversized"):
                repair.plan(self.folder)
        # Allow an exact-size rewritten sidecar, but reject it one byte below that limit.
        _, changes = repair.plan(self.folder)
        limit = len(changes[0].updated)
        with patch.object(repair, "JSON_LIMIT", limit):
            self.assertEqual(len(repair.plan(self.folder)[1]), 1)
        with patch.object(repair, "JSON_LIMIT", limit - 1):
            with self.assertRaisesRegex(RepairError, "Rewritten"):
                repair.plan(self.folder)
        self.assertFalse(self.manifest.exists())

    def test_symlink_substitution_after_planning_is_refused(self):
        media, sidecar = self.pair()
        _, changes = repair.plan(self.folder)
        for path in (media, sidecar):
            with self.subTest(path=path):
                content = path.read_bytes()
                outside = self.root / "replacement"
                outside.write_bytes(content)
                path.unlink()
                path.symlink_to(outside)
                with self.assertRaises(RepairError):
                    repair.apply(changes[0])
                self.assertFalse(media.with_suffix(".jpg").exists())
                path.unlink()
                path.write_bytes(content)

    def test_interruption_after_unlink_restores_original_pair(self):
        _, sidecar = self.pair()
        sidecar.chmod(0o640)
        before = snapshot(self.folder)
        _, changes = repair.plan(self.folder)
        unlink = Path.unlink

        def interrupt_after_removal(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path == sidecar:
                raise KeyboardInterrupt()
            return result

        with patch.object(Path, "unlink", interrupt_after_removal):
            with self.assertRaises(KeyboardInterrupt):
                repair.apply(changes[0])
        self.assertEqual(snapshot(self.folder), before)
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o640)

    def test_partial_batch_can_finish_with_new_manifest_without_touching_completed_pairs(self):
        self.pair("a.HEIC")
        self.pair("z.HEIC")
        original_apply = repair.apply

        def fail_second(change):
            if change.media.name == "z.HEIC":
                raise OSError("second file failure")
            original_apply(change)

        with patch.object(repair, "apply", fail_second), self.assertRaises(OSError):
            self.run_repair()
        old_manifest = self.manifest.read_bytes()
        completed = (self.folder / "a.jpg.JSON").read_bytes()
        self.assertEqual(len(json.loads(old_manifest)["changes"]), 2)
        self.manifest = self.root / "remaining.json"
        result = self.run_repair()
        self.assertEqual([r["old_path"] for r in result["changes"]], ["z.HEIC"])
        self.assertEqual((self.root / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual((self.folder / "a.jpg.JSON").read_bytes(), completed)
        self.assertTrue((self.folder / "z.jpg").is_file())

    def test_cli_dry_run_apply_required_manifest_and_interrupt(self):
        self.pair()
        args = [str(self.folder), "--manifest", str(self.manifest)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(repair.main(args + ["--dry-run"]), 0)
            self.assertFalse(self.manifest.exists())
            self.assertTrue((self.folder / "photo.HEIC").is_file())
            self.assertEqual(repair.main(args), 0)
            with self.assertRaises(SystemExit) as error:
                repair.main([str(self.folder)])
            self.assertEqual(error.exception.code, 2)
            with patch.object(repair, "repair", side_effect=KeyboardInterrupt):
                self.assertEqual(repair.main(args), 130)

    def test_header_inspection_reads_only_a_small_prefix(self):
        media, _ = self.pair()
        original_open = Path.open

        class Header(io.BytesIO):
            def read(stream, size=-1):
                self.assertIsInstance(size, int)
                self.assertGreater(size, 0)
                self.assertLessEqual(size, 32)
                return super().read(size)

        def header_only(path, *args, **kwargs):
            if path == media and args == ("rb",):
                return Header(b"\xff\xd8\xffpayload")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", header_only):
            self.assertEqual(corrected_suffix(media), ".jpg")

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_repaired_sidecar_survives_real_immich_go_folder_reader(self):
        self.pair()
        self.run_repair()
        before = snapshot(self.folder)
        config = self.root / "empty.json"
        config.write_text("{}")
        result = subprocess.run(
            [
                "immich-go",
                "archive",
                "from-folder",
                "--config",
                str(config),
                "--write-to-folder",
                str(self.root / "roundtrip"),
                "--save-config=false",
                "--log-file",
                str(self.root / "reader.log"),
                "--",
                str(self.folder),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        sidecars = list((self.root / "roundtrip").rglob("*.JSON"))
        self.assertEqual(len(sidecars), 1)
        actual = json.loads(sidecars[0].read_text())
        for field in (
            "fileName",
            "dateTaken",
            "description",
            "latitude",
            "longitude",
            "favorited",
            "albums",
        ):
            expected = "photo.jpg" if field == "fileName" else self.metadata[field]
            self.assertEqual(actual[field], expected)
        self.assertEqual(sidecars[0].with_suffix("").read_bytes(), before["photo.jpg"])
        self.assertEqual(snapshot(self.folder), before)


@given(st.binary(max_size=1024), st.text(max_size=40))
@settings(max_examples=20)
def check_repair_round_trip(payload, description):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        folder = root / "media"
        folder.mkdir()
        media = folder / "image.HEIC"
        content = b"\xff\xd8\xff" + payload
        media.write_bytes(content)
        metadata = {
            "software": "immich-go version:0.32.0",
            "fileName": "alias",
            "description": description,
        }
        (folder / "image.HEIC.JSON").write_text(json.dumps(metadata))
        with redirect_stdout(io.StringIO()):
            repair.repair(folder, root / "mapping.json")
        assert (folder / "image.jpg").read_bytes() == content
        assert json.loads((folder / "image.jpg.JSON").read_text()) == dict(
            metadata, fileName="image.jpg"
        )
