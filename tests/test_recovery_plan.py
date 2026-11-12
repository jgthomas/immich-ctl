"""Saved companion plans preserve dry-run decisions without repeating expensive work."""

import copy
import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch

from immich_ctl import companion_recover as cr
from immich_ctl import recovery_plan as plans
from immich_ctl.takeout_recover import RecoveryError
from tests import test_companion_recover as fixtures


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.CompanionTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.plan = self.fx.root / "plan.json"

    def build(self):
        return self.fx.run_recovery(dry_run=True, save_plan=self.plan)

    def apply(self, **options):
        return cr.recover_companions(
            [self.fx.archive],
            [self.fx.folder],
            self.fx.output,
            self.fx.selection,
            apply_plan=self.plan,
            **options,
        )

    def rewrite(self, change):
        data = json.loads(self.plan.read_text())
        change(data["payload"])
        data["sha256"] = hashlib.sha256(plans.encoded(data["payload"])).hexdigest()
        self.plan.write_text(json.dumps(data))

    def assert_unpublished(self):
        self.assertFalse(self.fx.output.exists())
        self.assertFalse(self.fx.manifest.exists())
        self.assertFalse(list(self.fx.root.glob(".immich-companions-*")))

    def test_apply_uses_saved_decisions_without_hashing_stills_scanning_or_probing(self):
        expected = self.build()
        self.assert_unpublished()
        self.assertEqual(self.plan.stat().st_mode & 0o777, 0o600)
        with (
            patch.object(cr, "local_files", side_effect=AssertionError("rehashing stills")),
            patch.object(cr, "inspect_archives", side_effect=AssertionError("rescanning JSON")),
            patch.object(cr, "probe", side_effect=AssertionError("reprobing videos")),
            patch.object(cr.shutil, "which", side_effect=AssertionError("ffprobe needed")),
        ):
            actual = self.apply()
        self.assertEqual(actual["files"], expected["files"])
        self.assertEqual(actual["counts"], expected["counts"])
        self.assertFalse(actual["dry_run"])
        row = actual["files"][0]
        target = self.fx.output / row["output_name"]
        self.assertEqual(target.read_bytes(), b"video")
        self.assertEqual(
            json.loads(target.with_name(target.name + ".JSON").read_text()), row["metadata"]
        )
        self.assertEqual(json.loads(self.fx.manifest.read_text())["state"], "complete")

    def test_changed_media_is_rejected_even_with_restored_size_and_mtime(self):
        self.build()
        old = self.fx.photo.stat()
        self.fx.photo.write_bytes(b"X" * old.st_size)
        os.utime(self.fx.photo, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaisesRegex(RecoveryError, "changed"):
            self.apply()
        self.assert_unpublished()

    def test_sidecar_hash_detects_changes_independently_of_file_timestamps(self):
        sidecar = self.fx.photo.with_name(self.fx.photo.name + ".JSON")
        original_fingerprint = plans.fingerprint
        stable = original_fingerprint(sidecar)

        def fingerprint(path):
            return stable if path == sidecar else original_fingerprint(path)

        with patch.object(plans, "fingerprint", side_effect=fingerprint):
            self.build()
            sidecar.write_text(sidecar.read_text().replace("Trip", "Tour"))
            with self.assertRaisesRegex(RecoveryError, "changed"):
                self.apply()
        self.assert_unpublished()

    def test_nested_folder_contents_are_part_of_the_snapshot(self):
        nested = self.fx.folder / "a-nested-folder"
        nested.mkdir()
        self.fx.photo.rename(nested / self.fx.photo.name)
        self.fx.photo.with_name(self.fx.photo.name + ".JSON").rename(nested / "photo.HEIC.JSON")
        self.build()
        (nested / "new.jpg").write_bytes(b"new media")
        with self.assertRaisesRegex(RecoveryError, "changed"):
            self.apply()
        self.assert_unpublished()

    def test_every_extracted_folder_is_checked_when_multiple_are_supplied(self):
        extra = self.fx.root / "z-other-extraction"
        extra.mkdir()
        other = extra / "other.HEIC"
        other.write_bytes(b"another still")
        self.fx.metadata(fixtures.native(fileName=other.name), path=other)
        folders = [self.fx.folder, extra]
        with (
            patch.object(cr, "probe", return_value=fixtures.INFO),
            patch.object(cr.shutil, "which", return_value="ffprobe"),
        ):
            cr.recover_companions(
                [self.fx.archive],
                folders,
                self.fx.output,
                self.fx.selection,
                dry_run=True,
                save_plan=self.plan,
            )
        self.fx.photo.write_bytes(b"changed first folder")
        with self.assertRaisesRegex(RecoveryError, "changed"):
            cr.recover_companions(
                [self.fx.archive], folders, self.fx.output, self.fx.selection, apply_plan=self.plan
            )
        self.assert_unpublished()

    def test_plan_checksum_detects_edited_decisions_and_json_key_order_is_irrelevant(self):
        self.build()
        data = json.loads(self.plan.read_text())
        self.plan.write_text(json.dumps(data, sort_keys=True, indent=4))
        plans.load(self.plan)
        data["payload"]["report"]["files"][0]["metadata"]["description"] = "edited plan"
        self.plan.write_text(json.dumps(data))
        with self.assertRaises(RecoveryError):
            self.apply()
        self.assert_unpublished()

    def test_plan_paths_cannot_use_symlink_parents_or_missing_parents(self):
        link = self.fx.root / "linked-folder"
        link.symlink_to(self.fx.root, target_is_directory=True)
        for path in (link / "plan.json", self.fx.root / "missing" / "plan.json", self.fx.folder):
            with self.subTest(path=path), self.assertRaises(RecoveryError):
                plans.destination(path, [self.fx.archive], [self.fx.folder], self.fx.output)

    def test_snapshot_rejects_nonregular_archives_and_missing_or_symlink_folders(self):
        with self.assertRaises(RecoveryError):
            plans.fingerprint(self.fx.folder)
        link = self.fx.root / "linked-folder"
        link.symlink_to(self.fx.folder, target_is_directory=True)
        for folder in (link, self.fx.root / "missing"):
            with self.subTest(folder=folder), self.assertRaises(RecoveryError):
                plans.snapshot([self.fx.archive], [folder])

    def test_direct_plan_save_is_exclusive_and_oversized_plans_are_rejected(self):
        self.build()
        original = self.plan.read_bytes()
        with self.assertRaises(FileExistsError):
            plans.save(self.plan, {}, {})
        self.assertEqual(self.plan.read_bytes(), original)
        with patch.object(plans, "LIMIT", 100):
            with self.assertRaises(RecoveryError):
                plans.load(self.plan)
            with self.assertRaises(RecoveryError):
                plans.save(self.fx.root / "too-large.json", {}, {"data": "x" * 200})
        self.assertFalse((self.fx.root / "too-large.json").exists())

    def test_changed_sidecar_or_folder_roster_invalidates_plan(self):
        sidecar = self.fx.photo.with_name(self.fx.photo.name + ".JSON")
        for operation in ("edit", "add", "remove", "rename", "symlink"):
            with self.subTest(operation=operation):
                if self.plan.exists():
                    self.plan.unlink()
                self.build()
                original = sidecar.read_bytes()
                added = self.fx.folder / "unexpected.jpg"
                renamed = sidecar.with_name("renamed.JSON")
                if operation == "edit":
                    sidecar.write_bytes(original.replace(b"Trip", b"Tour"))
                elif operation == "add":
                    added.write_bytes(b"unrelated")
                elif operation == "remove":
                    sidecar.unlink()
                elif operation == "rename":
                    sidecar.rename(renamed)
                else:
                    added.symlink_to(self.fx.photo)
                with self.assertRaises(RecoveryError):
                    self.apply()
                self.assert_unpublished()
                if added.exists() or added.is_symlink():
                    added.unlink()
                if renamed.exists():
                    renamed.unlink()
                sidecar.write_bytes(original)

    def test_changed_or_added_archive_invalidates_plan(self):
        self.build()
        self.fx.write(entries={"Photos from 2025/photo": b"changed video"})
        with self.assertRaisesRegex(RecoveryError, "changed"):
            self.apply()
        self.assert_unpublished()

    def test_changed_selection_folders_archive_set_output_or_policy_cannot_reuse_plan(self):
        self.build()
        extra = self.fx.root / "part2.zip"
        self.fx.write(extra, {})
        cases = [
            (
                [self.fx.archive],
                [self.fx.folder],
                self.fx.output,
                (date(2025, 1, 1), date(2026, 12, 31)),
                False,
            ),
            ([self.fx.archive], [], self.fx.output, self.fx.selection, False),
            ([self.fx.archive, extra], [self.fx.folder], self.fx.output, self.fx.selection, False),
            ([self.fx.archive], [self.fx.folder], self.fx.root / "other", self.fx.selection, False),
            ([self.fx.archive], [self.fx.folder], self.fx.output, self.fx.selection, True),
        ]
        for paths, folders, output, dates, include in cases:
            with self.subTest(paths=paths, folders=folders, output=output, include=include):
                with self.assertRaises(RecoveryError):
                    cr.recover_companions(
                        paths,
                        folders,
                        output,
                        dates,
                        include_unpaired=include,
                        apply_plan=self.plan,
                    )
                self.assertFalse(output.exists())
        self.assert_unpublished()

    def test_changed_inputs_during_planning_do_not_publish_a_plan(self):
        def probe(*args):
            self.fx.photo.write_bytes(b"changed while probing")
            return fixtures.INFO

        with (
            patch.object(cr, "probe", side_effect=probe),
            patch.object(cr.shutil, "which", return_value="ffprobe"),
        ):
            with self.assertRaisesRegex(RecoveryError, "changed"):
                cr.recover_companions(
                    [self.fx.archive],
                    [self.fx.folder],
                    self.fx.output,
                    self.fx.selection,
                    dry_run=True,
                    save_plan=self.plan,
                )
        self.assertFalse(self.plan.exists())
        self.assert_unpublished()

    def test_original_recovery_without_a_plan_does_not_take_snapshots(self):
        with patch.object(plans, "snapshot", side_effect=AssertionError("unexpected snapshot")):
            self.fx.run_recovery(dry_run=True)

    def test_each_planned_metadata_field_and_later_rows_are_validated(self):
        self.build()
        original = self.plan.read_bytes()
        for field, value in (
            ("fileName", "wrong.MOV"),
            ("software", "unknown"),
            ("dateTaken", "2026-01-01T00:00:00Z"),
        ):
            with self.subTest(field=field):
                self.plan.write_bytes(original)

                def edit(payload, field=field, value=value):
                    payload["report"]["files"][0]["metadata"][field] = value
                    covered = copy.deepcopy(payload["report"]["files"][0])
                    covered["status"] = "covered"
                    payload["report"]["files"].insert(0, covered)
                    payload["report"]["counts"] = {"covered": 1, "recover": 1}

                self.rewrite(edit)
                with patch.object(cr, "publish") as publish:
                    with self.assertRaises(RecoveryError):
                        self.apply()
                    publish.assert_not_called()

    def test_duplicate_names_and_duplicate_checksums_are_independently_rejected(self):
        self.build()
        original = self.plan.read_bytes()
        for field, value in (("sha256", "a" * 64), ("output_name", "another.MOV")):
            with self.subTest(field=field):
                self.plan.write_bytes(original)

                def duplicate(payload, field=field, value=value):
                    row = copy.deepcopy(payload["report"]["files"][0])
                    row[field] = value
                    row["metadata"]["fileName"] = row["output_name"]
                    payload["report"]["files"].append(row)
                    payload["report"]["counts"] = {"recover": 2}

                self.rewrite(duplicate)
                with patch.object(cr, "publish") as publish:
                    with self.assertRaises(RecoveryError):
                        self.apply()
                    publish.assert_not_called()

    def test_non_dry_run_reports_cannot_be_reused_as_plans(self):
        self.build()
        self.rewrite(lambda p: p["report"].update(dry_run=False))
        with self.assertRaises(RecoveryError):
            self.apply()
        self.assert_unpublished()

    def test_apply_preserves_inclusive_date_boundaries(self):
        self.fx.selection = (date(2025, 6, 15), date(2025, 6, 15))
        self.build()
        self.assertEqual(self.apply()["counts"], {"recover": 1})

    def test_changed_inputs_during_extraction_roll_back_staging(self):
        self.build()
        original = cr.shutil.copyfileobj

        def copy_and_change(*args):
            original(*args)
            self.fx.photo.write_bytes(b"changed during extraction")

        with patch.object(cr.shutil, "copyfileobj", side_effect=copy_and_change):
            with self.assertRaisesRegex(RecoveryError, "changed"):
                self.apply()
        self.assert_unpublished()

    def test_source_byte_and_member_checks_are_still_enforced(self):
        self.build()
        original = self.plan.read_bytes()
        for change in ({"sha256": "a" * 64}, {"member": "wrong"}, {"entry_index": 999}):
            with self.subTest(change=change):
                self.plan.write_bytes(original)
                self.rewrite(lambda p, change=change: p["report"]["files"][0].update(change))
                with self.assertRaises(RecoveryError):
                    self.apply()
                self.assert_unpublished()

    def test_malformed_unsafe_or_conflicting_rows_are_rejected_before_publish(self):
        self.build()
        original = self.plan.read_bytes()
        bad_rows = [
            {"output_name": "../escape.MOV"},
            {"output_name": "/escape.MOV"},
            {"output_name": "..\\escape.MOV"},
            {"archive": "/other.zip"},
            {"entry_index": -1},
            {"entry_index": True},
            {"size": -1},
            {"crc32": -1},
            {"sha256": "not a checksum"},
            {"metadata": {}},
        ]
        for change in bad_rows:
            with self.subTest(change=change):
                self.plan.write_bytes(original)
                self.rewrite(lambda p, change=change: p["report"]["files"][0].update(change))
                with patch.object(cr, "publish") as publish:
                    with self.assertRaises(RecoveryError):
                        self.apply()
                    publish.assert_not_called()
        self.plan.write_bytes(original)

        def duplicate(payload):
            row = copy.deepcopy(payload["report"]["files"][0])
            payload["report"]["files"].append(row)
            payload["report"]["counts"] = {"recover": 2}

        self.rewrite(duplicate)
        with self.assertRaises(RecoveryError):
            self.apply()
        self.assert_unpublished()

    def test_incompatible_damaged_and_non_plan_json_are_rejected(self):
        self.build()
        original = self.plan.read_bytes()
        for raw in (b"not json", b"[]", b"{}", original.replace(b'"version":1', b'"version":99')):
            with self.subTest(raw=raw[:30]):
                self.plan.write_bytes(raw)
                with self.assertRaises(RecoveryError):
                    self.apply()
                self.assert_unpublished()
        self.plan.write_bytes(original)
        self.rewrite(lambda p: p.update(version=999))
        with self.assertRaises(RecoveryError):
            self.apply()

    def test_save_never_overwrites_and_plan_cannot_live_inside_inputs_or_outputs(self):
        self.build()
        original = self.plan.read_bytes()
        with self.assertRaises(RecoveryError):
            self.build()
        self.assertEqual(self.plan.read_bytes(), original)
        for path in (
            self.fx.archive,
            self.fx.photo.with_name(self.fx.photo.name + ".JSON"),
            self.fx.output,
            self.fx.manifest,
        ):
            with self.subTest(path=path):
                with self.assertRaises(RecoveryError):
                    self.fx.run_recovery(dry_run=True, save_plan=path)
        self.assert_unpublished()

    def test_invalid_flag_combinations_and_empty_plan(self):
        for options in (
            {"save_plan": self.plan},
            {"dry_run": True, "apply_plan": self.plan},
            {"save_plan": self.plan, "apply_plan": self.plan, "dry_run": True},
        ):
            with self.subTest(options=options), self.assertRaises(RecoveryError):
                cr.recover_companions(
                    [self.fx.archive],
                    [self.fx.folder],
                    self.fx.output,
                    self.fx.selection,
                    **options,
                )
        self.fx.entries = {}
        self.fx.write()
        self.build()
        self.assertEqual(self.apply()["counts"], {})
        self.assert_unpublished()

    def test_cli_save_and_apply(self):
        args = [
            "recover-companions",
            "--date-range",
            "2025",
            "--extracted",
            str(self.fx.folder),
            "--output",
            str(self.fx.output),
            str(self.fx.archive),
        ]
        output = io.StringIO()
        with (
            patch.object(cr, "probe", return_value=fixtures.INFO),
            patch.object(cr.shutil, "which", return_value="ffprobe"),
            redirect_stdout(output),
        ):
            self.assertEqual(
                fixtures.takeout.main([*args, "--dry-run", "--save-plan", str(self.plan)]), 0
            )
        self.assertIn("plan saved", output.getvalue())
        with (
            redirect_stdout(io.StringIO()),
            patch.object(cr, "probe", side_effect=AssertionError()),
        ):
            self.assertEqual(fixtures.takeout.main([*args, "--apply-plan", str(self.plan)]), 0)
        self.assertTrue(self.fx.output.exists())
