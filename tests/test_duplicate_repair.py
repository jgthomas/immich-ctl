"""Duplicate consolidation preserves album payloads and backs up original bytes."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from immich_ctl import duplicate_repair
from immich_ctl.duplicate_repair import main


class DuplicateRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / "extracted"
        self.folder.mkdir()
        self.backup = self.folder.with_name("extracted-duplicate-backup")

    def add(self, name, albums, **fields):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = fields.pop("contents", b"same photo")
        path.write_bytes(contents)
        sidecar = path.with_name(path.name + ".JSON")
        sidecar.write_text(
            json.dumps(
                {
                    "software": "immich-go version:0.32.0",
                    "fileName": path.name,
                    "dateTaken": "2017-06-01T12:00:00Z",
                    "albums": albums,
                    **fields,
                }
            )
        )
        return path, sidecar

    def invoke(self, *options):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            result = main([str(self.folder), *options])
        return result, errors.getvalue()

    def test_keeps_album_bearing_copy_and_moves_only_byte_identical_pair(self):
        extra = self.add("a.jpg", [])
        keep = self.add("z.jpg", [{"title": "Trip", "description": "Keep me"}])
        unrelated = self.add("other.jpg", [], contents=b"other file")
        paths = extra + keep + unrelated
        before = {p: p.read_bytes() for p in paths}
        self.assertEqual(self.invoke("--dry-run"), (0, ""))
        self.assertFalse(self.backup.exists())
        self.assertEqual({p: p.read_bytes() for p in paths}, before)
        self.assertEqual(self.invoke(), (0, ""))
        for p in extra:
            self.assertFalse(p.exists())
            self.assertEqual((self.backup / p.name).read_bytes(), before[p])
        for p in keep + unrelated:
            self.assertEqual(p.read_bytes(), before[p])
        manifest = json.loads((self.backup / "manifest.json").read_bytes())
        self.assertEqual(manifest["source"], str(self.folder))
        (group,) = manifest["groups"]
        self.assertEqual(group["retain"]["media"], keep[0].name)
        self.assertEqual(group["sha256"], hashlib.sha256(before[keep[0]]).hexdigest())
        self.assertEqual(group["albums"], ["Trip"])
        self.assertEqual(
            group["retain"]["sidecar_sha256"], hashlib.sha256(before[keep[1]]).hexdigest()
        )
        self.assertEqual(self.invoke(), (0, ""))

    def test_refuses_split_albums_conflicting_payloads_or_different_sidecar_metadata(self):
        for albums, fields in (
            ([{"title": "Family"}], {}),
            ([{"title": "Trip", "description": "Different"}], {}),
            ([], {"description": "Different"}),
        ):
            with self.subTest(albums=albums, fields=fields):
                keep = self.add("a.jpg", [{"title": "Trip"}])
                extra = self.add("b.jpg", albums, **fields)
                before = {p: p.read_bytes() for p in keep + extra}
                code, error = self.invoke()
                self.assertEqual(code, 1)
                self.assertIn("requires manual review", error)
                self.assertFalse(self.backup.exists())
                self.assertEqual({p: p.read_bytes() for p in keep + extra}, before)

    def test_nested_backup_accepts_omitted_albums_and_json_key_order(self):
        extra, sidecar = self.add("a.jpg", [])
        keep = self.add("z.jpg", [{"title": "Trip", "description": "Keep"}], description="Photo")
        data = json.loads(sidecar.read_bytes())
        del data["albums"]
        sidecar.write_text(json.dumps({"description": "Photo", **data}))
        nested = self.folder / "photos" / "june"
        nested.mkdir(parents=True)
        for p in (extra, sidecar):
            p.rename(nested / p.name)
        self.assertEqual(self.invoke(), (0, ""))
        for p in (extra, sidecar):
            self.assertTrue((self.backup / "photos" / "june" / p.name).is_file())
        self.assertTrue(all(p.is_file() for p in keep))

    def test_refuses_unusable_sidecars_without_moving_any_files(self):
        for albums, fields in (
            ({}, {}),
            ([{"title": 1}], {}),
            ([{"title": "Trip"}, {"title": "Trip"}], {}),
            ([], {"fileName": "wrong.jpg"}),
            ([], {"software": "wrong"}),
        ):
            with self.subTest(albums=albums, fields=fields):
                keep = self.add("a.jpg", [{"title": "Trip"}])
                extra = self.add("b.jpg", albums, **fields)
                self.assertEqual(self.invoke()[0], 1)
                self.assertFalse(self.backup.exists())
                self.assertTrue(all(p.is_file() for p in keep + extra))
        extra = self.add("b.jpg", [])
        content = extra[1].read_text()
        extra[1].write_text(
            content.replace('{"software"', '{"albums": [{"title": "Hidden"}], "software"')
        )
        self.assertEqual(self.invoke()[0], 1)
        self.assertFalse(self.backup.exists())

    def test_existing_backup_prevents_changes(self):
        self.add("a.jpg", [])
        self.add("b.jpg", [{"title": "Trip"}])
        self.backup.mkdir()
        before = sorted(self.folder.iterdir())
        code, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("Backup already exists", error)
        self.assertEqual(sorted(self.folder.iterdir()), before)
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_sidecar_read_keeps_the_size_limit_and_rejects_nonfinite_json(self):
        media, sidecar = self.add("a.jpg", [])
        raw = sidecar.read_bytes()
        open_file = Path.open

        class BoundedStream:
            def __enter__(stream):
                stream.file = open_file(sidecar, "rb")
                return stream

            def __exit__(stream, *_):
                stream.file.close()

            def read(stream, size=-1):
                self.assertGreater(size, 0)
                self.assertLessEqual(size, len(raw) + 1)
                return stream.file.read(size)

        with (
            patch.object(duplicate_repair, "JSON_LIMIT", len(raw)),
            patch.object(Path, "open", return_value=BoundedStream()),
        ):
            self.assertEqual(duplicate_repair.read_sidecar(media)[1], raw)
            with open_file(sidecar, "ab") as stream:
                stream.write(b" ")
            with self.assertRaisesRegex(duplicate_repair.RepairError, "Oversized"):
                duplicate_repair.read_sidecar(media)

        self.add("a.jpg", [], latitude=float("nan"))
        with self.assertRaisesRegex(duplicate_repair.RepairError, "Non-standard JSON"):
            duplicate_repair.read_sidecar(media)

    def test_refuses_glob_paths_and_sidecar_marker_hidden_by_json_escape(self):
        media, _ = self.add("a[1].jpg", [{"title": "Trip"}])
        with self.assertRaises(duplicate_repair.RepairError):
            duplicate_repair.read_sidecar(media)
        media, sidecar = self.add("a.jpg", [{"title": "Trip"}])
        sidecar.write_bytes(
            sidecar.read_bytes().replace(b"immich-go version", b"immich-go\\u0020version")
        )
        with self.assertRaises(duplicate_repair.RepairError):
            duplicate_repair.read_sidecar(media)

    def test_failure_moving_sidecar_restores_media(self):
        extra = self.add("a.jpg", [])
        keep = self.add("b.jpg", [{"title": "Trip"}])
        before = {p: p.read_bytes() for p in extra + keep}
        rename = Path.rename

        def fail_sidecar(path, target):
            if path == extra[1]:
                raise OSError("simulated sidecar move failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_sidecar):
            code, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("simulated sidecar move failure", error)
        self.assertEqual({p: p.read_bytes() for p in extra + keep}, before)

    def test_changed_plan_inputs_are_rejected_before_backup_creation(self):
        extra = self.add("a.jpg", [])
        keep = self.add("b.jpg", [{"title": "Trip"}])
        _, groups, snapshots = duplicate_repair.plan(self.folder)
        keep[1].write_text("{}")
        with self.assertRaisesRegex(duplicate_repair.RepairError, "Input changed"):
            duplicate_repair.apply(self.folder, self.backup, groups, snapshots)
        self.assertFalse(self.backup.exists())
        self.assertTrue(all(p.is_file() for p in extra + keep))

    def test_rollback_preserves_a_file_created_at_the_original_path(self):
        extra = self.add("a.jpg", [])
        self.add("b.jpg", [{"title": "Trip"}])
        rename = Path.rename

        def occupy_original_path(path, target):
            if path == extra[1]:
                extra[0].write_bytes(b"new file")
                raise OSError("simulated sidecar move failure")
            return rename(path, target)

        with patch.object(Path, "rename", occupy_original_path):
            code, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("Cannot restore", error)
        self.assertEqual(extra[0].read_bytes(), b"new file")
        self.assertEqual((self.backup / extra[0].name).read_bytes(), b"same photo")

    def test_keeper_must_be_uploadable_and_have_all_albums(self):
        album = [{"title": "Trip"}]
        excluded = self.add("Recently Deleted/IMG.jpg", album)
        eligible = self.add("Timeline/IMG.jpg", [])
        code, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("no uploadable", error)
        self.assertFalse(self.backup.exists())
        self.assertTrue(all(path.is_file() for path in excluded + eligible))

        self.add("Timeline/IMG.jpg", album)
        self.assertEqual(self.invoke(), (0, ""))
        self.assertTrue(all(path.is_file() for path in eligible))
        self.assertTrue(
            all((self.backup / path.relative_to(self.folder)).is_file() for path in excluded)
        )

    def test_chooses_unique_keeper_over_name_time_collision(self):
        album = [{"title": "Trip"}]
        risky = self.add("a/IMG.jpg", album)
        safe = self.add("b/IMG-other.jpg", [])
        other = self.add(
            "c/IMG.jpg", [], contents=b"different photo", dateTaken="2017-06-01T12:00:05Z"
        )
        code, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("collision-free", error)
        self.assertFalse(self.backup.exists())
        self.assertTrue(all(path.is_file() for path in risky + safe + other))

        self.add("b/IMG-other.jpg", album)
        self.assertEqual(self.invoke(), (0, ""))
        self.assertTrue(all(path.is_file() for path in safe + other))
        self.assertFalse(risky[0].exists())

    def test_keeper_checks_filename_alias_but_allows_known_distant_dates(self):
        keep = self.add("a/IMG.jpg", [{"title": "Trip"}])
        extra = self.add("b/IMG-copy.jpg", [])
        other = self.add("c/other.jpg", [], contents=b"different photo", fileName="IMG.jpg")
        self.assertEqual(self.invoke()[0], 1)
        self.assertFalse(self.backup.exists())
        self.assertTrue(all(path.is_file() for path in keep + extra + other))

        self.add(
            "c/other.jpg",
            [],
            contents=b"different photo",
            fileName="IMG.jpg",
            dateTaken="2017-06-01T12:00:06Z",
        )
        self.assertEqual(self.invoke(), (0, ""))
        self.assertTrue(all(path.is_file() for path in keep + other))
        self.assertFalse(extra[0].exists())

    def test_refuses_go_case_collisions_in_both_key_orders_and_nested_albums(self):
        keep = self.add("a.jpg", [{"title": "Trip"}])
        extra = self.add("b.jpg", [{"title": "Trip"}])
        for case in ("first", "last", "nested"):
            with self.subTest(case=case):
                original = {
                    "software": "immich-go version:0.32.0",
                    "fileName": "a.jpg",
                    "dateTaken": "2017-06-01T12:00:00Z",
                    "albums": [{"title": "Trip"}],
                }
                if case == "first":
                    original = {"Albums": None, **original}
                elif case == "last":
                    original["Albums"] = None
                else:
                    original["albums"][0]["Title"] = "Wrong"
                keep[1].write_text(json.dumps(original))
                code, error = self.invoke()
                self.assertEqual(code, 1)
                self.assertIn("case-colliding JSON key", error)
                self.assertFalse(self.backup.exists())
                self.assertTrue(all(path.is_file() for path in keep + extra))

    def test_move_verifies_both_media_and_sidecar_and_rolls_back_mismatch(self):
        extra = self.add("a.jpg", [])
        keep = self.add("b.jpg", [{"title": "Trip"}])
        rename = Path.rename
        for replaced in extra:
            with self.subTest(replaced=replaced):
                original = replaced.read_bytes()

                def replace_before_move(path, target, replaced=replaced):
                    if path == replaced and target.is_relative_to(self.backup):
                        path.unlink()
                        path.write_bytes(b"replacement")
                    return rename(path, target)

                # Use a different destination for each run so the first
                # failed run's manifest can remain available.
                plan = duplicate_repair.plan(self.folder)
                _, groups, snapshots = plan
                backup = self.backup / replaced.name
                self.backup.mkdir(exist_ok=True)
                with (
                    patch.object(Path, "rename", replace_before_move),
                    self.assertRaisesRegex(
                        duplicate_repair.RepairError, "differs from the repair plan"
                    ),
                ):
                    duplicate_repair.apply(self.folder, backup, groups, snapshots)
                self.assertEqual(replaced.read_bytes(), b"replacement")
                self.assertTrue(all(path.is_file() for path in extra + keep))
                self.assertFalse((backup / replaced.name).exists())
                replaced.write_bytes(original)
