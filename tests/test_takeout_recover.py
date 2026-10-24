"""Recovery planning, byte preservation, metadata round trips and failure safeguards."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.takeout_recover import recover
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

takeout = load_script("immich-takeout")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "takeout.zip"
        self.extracted = self.root / "extracted"
        self.extracted.mkdir()
        (self.extracted / "renamed-original.jpg").write_bytes(b"original")
        self.output = self.root / "recovery"
        self.manifest = self.root / "recovery.manifest.json"
        self.selection = (date(2023, 1, 1), date(2023, 12, 31))
        self.entries = {
            "Photos from 2023/photo(1).jpg": b"original",
            "Photos from 2023/photo.jpg.supplemental-metadata(1).json": sidecar("photo.jpg"),
            "Photos from 2023/photo-edited(1).jpg": b"edited",
        }
        self.write()

    def write(self):
        write_zip(self.archive, self.entries)

    def invoke(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = takeout.main(
                [
                    "recover",
                    "--date-range",
                    "2023",
                    "--extracted",
                    str(self.extracted),
                    "--output",
                    str(self.output),
                    *map(str, extra),
                    str(self.archive),
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_does_not_write_or_invoke_tool_and_counts_missing_bytes(self):
        before = self.archive.read_bytes()
        original_files = set(self.root.rglob("*"))
        with patch.object(takeout, "run_immich_go") as run:
            code, out, err = self.invoke("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("recover: 1; covered: 0", out)
        self.assertIn(hashlib.sha256(b"edited").hexdigest(), out)
        self.assertIn("no files written", out)
        run.assert_not_called()
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual(set(self.root.rglob("*")), original_files)

    def test_checksum_coverage_is_independent_of_name_and_not_metadata_confirmation(self):
        (self.extracted / "completely-different-name.jpg").write_bytes(b"edited")
        code, out, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertIn("recover: 0; covered: 1", out)
        self.assertIn("metadata and album coverage still need validation", out)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())

    def test_dates_conflicts_own_metadata_and_absent_original_are_not_guessed(self):
        for change, expected in (
            (
                {
                    "Photos from 2023/photo.jpg.supplemental-metadata(1).json": sidecar(
                        "photo.jpg", timestamp="1"
                    )
                },
                "outside_dates: 1",
            ),
            (
                {
                    "Photos from 2023/photo.jpg.supplemental-metadata(1).json": sidecar(
                        "photo.jpg", timestamp="0"
                    )
                },
                "unresolved: 1",
            ),
            (
                {"Photos from 2023/photo.jpg.json": sidecar("photo(1).jpg", "competitor")},
                "unresolved: 1",
            ),
            (
                {
                    "Photos from 2023/photo-edited(1).jpg.json": sidecar(
                        "photo-edited(1).jpg", "own", "0"
                    )
                },
                "unresolved: 1",
            ),
        ):
            with self.subTest(change=change):
                write_zip(self.archive, self.entries | change)
                code, out, err = self.invoke("--dry-run")
                self.assertEqual(code, 0, err)
                self.assertIn(expected, out)
        entries = dict(self.entries)
        del entries["Photos from 2023/photo(1).jpg"]
        write_zip(self.archive, entries)
        self.assertIn("unresolved: 1", self.invoke("--dry-run")[1])

    def test_differing_sidecar_contents_for_one_id_require_review(self):
        self.entries["Photos from 2023/photo(1).jpg.json"] = dict(
            sidecar("photo.jpg"), description="different"
        )
        self.write()
        code, out, err = self.invoke("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("unresolved: 1", out)
        self.assertIn("conflicting source sidecar", out)

    def test_date_range_includes_last_second_of_final_day_but_not_next_day(self):
        key = "Photos from 2023/photo.jpg.supplemental-metadata(1).json"
        for timestamp, status in (
            ("1704067199", "recover: 1; covered: 0; unresolved: 0; excluded: 0; outside_dates: 0"),
            ("1704067200", "recover: 0; covered: 0; unresolved: 0; excluded: 0; outside_dates: 1"),
        ):
            with self.subTest(timestamp=timestamp):
                write_zip(
                    self.archive, self.entries | {key: sidecar("photo.jpg", timestamp=timestamp)}
                )
                code, out, err = self.invoke("--dry-run")
                self.assertEqual(code, 0, err)
                self.assertIn(status, out)

    def test_skipping_an_earlier_edit_does_not_hide_later_recoverable_edits(self):
        for status in ("excluded", "covered", "conflicting", "outside_dates", "missing_original"):
            with self.subTest(status=status):
                entries = {"Year/": b""}
                for stem in ("a", "z"):
                    entries[f"Year/{stem}(1).jpg"] = (stem + " original").encode()
                    entries[f"Year/{stem}-edited(1).jpg"] = (stem + " edited").encode()
                    entries[f"Year/{stem}.jpg.supplemental-metadata(1).json"] = sidecar(
                        stem + ".jpg", stem
                    )
                key = "Year/a.jpg.supplemental-metadata(1).json"
                covered = self.extracted / "covered.jpg"
                covered.unlink(missing_ok=True)
                if status == "excluded":
                    entries[key]["trashed"] = True
                elif status == "covered":
                    covered.write_bytes(b"a edited")
                elif status == "conflicting":
                    entries["Year/a(1).jpg.json"] = entries[key] | {"description": "conflict"}
                elif status == "outside_dates":
                    entries[key] = sidecar("a.jpg", "a", "1")
                else:
                    del entries["Year/a(1).jpg"]
                write_zip(self.archive, entries)
                code, out, err = self.invoke("--dry-run")
                self.assertEqual(code, 0, err)
                self.assertIn("recover: 1;", out)
                rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
                later = next(row for row in rows if row["path"] == "Year/z-edited(1).jpg")
                self.assertEqual(later["status"], "recover")
                self.assertEqual(later["sha256"], hashlib.sha256(b"z edited").hexdigest())

    def test_unreadable_subdirectory_stops_recovery_before_extracting_or_writing(self):
        real_walk = os.walk

        def failing_walk(folder, *args, **kwargs):
            # Simulate a failure after os.walk has already found readable media.
            yield next(real_walk(folder, *args, **kwargs))
            onerror = kwargs.get("onerror")
            if onerror is not None:
                onerror(PermissionError("cannot read nested folder"))

        before = self.archive.read_bytes()
        with (
            patch("immich_ctl.takeout_recover.os.walk", side_effect=failing_walk),
            patch.object(takeout, "run_immich_go") as run,
        ):
            code, _, err = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("cannot read nested folder", err)
        run.assert_not_called()
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        self.assertFalse(list(self.root.glob(".immich-recovery-*")))

    def test_filters_exclude_trashed_and_partner_but_allow_archived(self):
        key = "Photos from 2023/photo.jpg.supplemental-metadata(1).json"
        for flags, arguments, status in (
            ({"trashed": True}, [], "excluded: 1"),
            ({"googlePhotosOrigin": {"fromPartnerSharing": {}}}, [], "excluded: 1"),
            (
                {"googlePhotosOrigin": {"fromPartnerSharing": {}}},
                ["--include-partner"],
                "recover: 1",
            ),
            ({"archived": True}, [], "recover: 1"),
        ):
            with self.subTest(flags=flags, arguments=arguments):
                write_zip(self.archive, self.entries | {key: self.entries[key] | flags})
                self.assertIn(status, self.invoke("--dry-run", *arguments)[1])

    def test_conflicting_album_copy_metadata_requires_review_even_across_zip_parts(self):
        base = self.entries["Photos from 2023/photo.jpg.supplemental-metadata(1).json"]
        other = self.root / "album.zip"
        for conflict in ({"description": "different"}, {"trashed": True}):
            for split in (False, True):
                with self.subTest(conflict=conflict, split=split):
                    album = {
                        "Trip/metadata.json": {"title": "Trip"},
                        "Trip/photo.jpg.json": base | conflict,
                    }
                    self.write()
                    if split:
                        write_zip(other, album)
                        arguments = [other]
                    else:
                        write_zip(self.archive, self.entries | album)
                        arguments = []
                    with patch.object(takeout, "run_immich_go") as run:
                        code, out, err = self.invoke(*arguments)
                    self.assertEqual(code, 0, err)
                    self.assertIn("recover: 0; covered: 0; unresolved: 1", out)
                    row = next(
                        json.loads(line) for line in out.splitlines() if line.startswith("{")
                    )
                    self.assertEqual(
                        row["reason"], "Missing or conflicting source sidecar contents"
                    )
                    self.assertEqual(
                        row["sidecars"],
                        [
                            "Photos from 2023/photo.jpg.supplemental-metadata(1).json",
                            "Trip/photo.jpg.json",
                        ],
                    )
                    run.assert_not_called()
                    self.assertFalse(self.output.exists())
                    self.assertFalse(self.manifest.exists())

    def test_identical_album_copy_is_accepted_without_a_local_media_match(self):
        base = self.entries["Photos from 2023/photo.jpg.supplemental-metadata(1).json"]
        write_zip(
            self.archive,
            self.entries
            | {
                "Trip/metadata.json": {"title": "Trip"},
                "Trip/photo.jpg.json": base,
            },
        )
        code, out, err = self.invoke("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("recover: 1; covered: 0; unresolved: 0", out)
        row = next(json.loads(line) for line in out.splitlines() if line.startswith("{"))
        self.assertEqual(row["albums"], ["Trip"])
        self.assertIn("Trip/photo.jpg.json", row["sidecars"])

    def test_default_log_from_inside_extraction_is_rejected_without_writes(self):
        previous = Path.cwd()
        before = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        try:
            os.chdir(self.extracted)
            with patch.object(takeout, "run_immich_go") as run:
                code, _, err = self.invoke()
            self.assertEqual(code, 1)
            self.assertIn("outside source and output folders", err)
            run.assert_not_called()
        finally:
            os.chdir(previous)
        self.assertEqual(
            before,
            {
                str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            },
        )
        self.assertFalse((self.extracted / "logs").exists())
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())

    def test_manifest_log_collisions_and_aliases_are_rejected_before_tool(self):
        alias = self.root / "manifest-alias"
        alias.symlink_to(self.manifest)
        for log in (self.manifest, self.manifest / "nested.log", alias):
            with self.subTest(log=log), patch.object(takeout, "run_immich_go") as run:
                code, _, err = self.invoke("--log-file", log)
                self.assertEqual(code, 1)
                self.assertIn("separate from the recovery manifest", err)
                run.assert_not_called()
                self.assertFalse(self.output.exists())
                self.assertFalse(self.manifest.exists())

    def test_default_log_is_resolved_once_and_passed_to_extractor(self):
        previous = Path.cwd()
        passed_logs = []

        def extract(args, paths):
            self.assertIsInstance(args.log_file, Path)
            self.assertTrue(args.log_file.is_absolute())
            self.assertEqual(args.log_file.parent, self.root / "logs")
            self.assertTrue(args.log_file.name.startswith("takeout-recover-"))
            passed_logs.append(args.log_file)
            return self.fake_extract(args, paths)

        try:
            os.chdir(self.root)
            with patch.object(takeout, "run_immich_go", side_effect=extract):
                code, _, err = self.invoke()
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(passed_logs), 1)

    def test_output_logs_and_symlinks_cannot_modify_inputs(self):
        for output in (self.extracted, self.extracted / "nested", self.root, self.archive):
            with self.subTest(output=output):
                previous, self.output = self.output, output
                try:
                    code, _, err = self.invoke("--dry-run")
                    self.assertEqual(code, 1)
                    self.assertIn("Error:", err)
                finally:
                    self.output = previous
        for log in (self.extracted / "new.log", self.output / "new.log", self.archive):
            self.assertEqual(self.invoke("--log-file", log)[0], 1)
        (self.extracted / "alias.jpg").symlink_to(self.archive)
        self.assertIn("Symlinks", self.invoke("--dry-run")[2])

    def test_existing_manifest_is_never_overwritten(self):
        self.manifest.write_text("previous")
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.manifest.read_text(), "previous")

    def test_identical_missing_bytes_with_conflicting_metadata_are_not_recovered(self):
        self.entries.update(
            {
                "Year/other(1).jpg": b"another original",
                "Year/other.jpg.supplemental-metadata(1).json": sidecar("other.jpg", "another-id"),
                "Year/other-edited(1).jpg": b"edited",
            }
        )
        self.write()
        code, out, err = self.invoke("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("recover: 0; covered: 0; unresolved: 2", out)
        self.assertIn("differing source metadata", out)

    def test_generated_sidecars_are_verified_before_any_output_is_published(self):
        for metadata in ({}, {"dateTaken": "2020-01-01T00:00:00Z"}):

            def fake_tool(args, paths, metadata=metadata):
                args.output.mkdir()
                media = args.output / "edited.jpg"
                media.write_bytes(b"edited")
                Path(str(media) + ".JSON").write_text(
                    json.dumps({"fileName": media.name} | metadata)
                )
                return 0

            with (
                self.subTest(metadata=metadata),
                patch.object(takeout, "run_immich_go", side_effect=fake_tool),
            ):
                self.assertEqual(self.invoke()[0], 1)
            self.assertFalse(self.output.exists())
            self.assertFalse(self.manifest.exists())

    def test_tool_failure_and_false_success_do_not_publish(self):
        def wrong_output(args, paths):
            args.output.mkdir()
            (args.output / "wrong.jpg").write_bytes(b"wrong")
            return 0

        for failure in (8, OSError("tool error"), KeyboardInterrupt()):
            with self.subTest(failure=failure):
                options = (
                    {"side_effect": failure}
                    if isinstance(failure, BaseException)
                    else {"return_value": failure}
                )
                with patch.object(takeout, "run_immich_go", **options):
                    code, _, _ = self.invoke()
                self.assertEqual(
                    code,
                    130
                    if isinstance(failure, KeyboardInterrupt)
                    else 1
                    if isinstance(failure, OSError)
                    else 8,
                )
                self.assertFalse(self.output.exists())
                self.assertFalse(self.manifest.exists())
        with patch.object(takeout, "run_immich_go", side_effect=wrong_output):
            code, _, err = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("planned checksums", err)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())

    def fake_extract(self, args, paths):
        import zipfile

        self.assertEqual(args.action, "extract")
        self.assertNotEqual(paths, [self.archive])
        with zipfile.ZipFile(paths[0]) as archive:
            entries = archive.namelist()
            media_name = next(name for name in entries if name.endswith(".jpg"))
            metadata = json.loads(archive.read(media_name + ".json"))
            self.assertEqual(archive.read(media_name), b"edited")
            self.assertEqual(metadata["title"], Path(media_name).name)
            self.assertEqual(metadata["url"], "photo")
        target = args.output / "2023/2023-01" / metadata["title"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"edited")
        Path(str(target) + ".JSON").write_text(
            json.dumps(
                {
                    "fileName": target.name,
                    "dateTaken": "2023-01-01T00:00:00Z",
                }
            )
        )
        return 0

    def test_successful_publish_and_manifest_without_installed_tool(self):
        before = self.archive.read_bytes()
        with patch.object(takeout, "run_immich_go", side_effect=self.fake_extract):
            code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.archive.read_bytes(), before)
        report = json.loads(self.manifest.read_text())
        row = report["files"][0]
        self.assertEqual(row["status"], "recover")
        self.assertEqual(row["path"], "Photos from 2023/photo-edited(1).jpg")
        self.assertEqual(len(row["recovered_paths"]), 1)
        self.assertEqual((self.output / row["recovered_paths"][0]).read_bytes(), b"edited")
        self.assertEqual((self.extracted / "renamed-original.jpg").read_bytes(), b"original")
        self.assertFalse(list(self.root.glob(".immich-recovery-*")))

    def test_copy_failure_cleans_only_our_outputs(self):
        with (
            patch.object(takeout, "run_immich_go", side_effect=self.fake_extract),
            patch("immich_ctl.takeout_recover.shutil.copytree", side_effect=OSError("copy failed")),
        ):
            code, _, err = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("copy failed", err)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        self.assertEqual((self.extracted / "renamed-original.jpg").read_bytes(), b"original")

    def test_output_created_during_extraction_survives_failed_publication(self):
        def extract(args, paths):
            code = self.fake_extract(args, paths)
            self.output.mkdir()
            (self.output / "unrelated.txt").write_bytes(b"keep this file")
            return code

        with patch.object(takeout, "run_immich_go", side_effect=extract):
            code, _, err = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        self.assertEqual((self.output / "unrelated.txt").read_bytes(), b"keep this file")
        self.assertEqual(list(self.output.iterdir()), [self.output / "unrelated.txt"])
        self.assertFalse(self.manifest.exists())
        self.assertFalse(list(self.root.glob(".immich-recovery-*")))

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_recovery_preserves_bytes_metadata_albums_and_folder_reader_compatibility(self):
        base = self.entries["Photos from 2023/photo.jpg.supplemental-metadata(1).json"]
        base.update(
            {
                "description": "Recovered edit",
                "geoDataExif": {"latitude": 51.5, "longitude": -0.12},
                "favorited": True,
                "archived": True,
            }
        )
        for album in ("Trip", "Family"):
            self.entries[album + "/metadata.json"] = {"title": album}
            self.entries[album + "/photo(1).jpg"] = b"original"
            self.entries[album + "/photo.jpg.supplemental-metadata(1).json"] = base
        self.write()
        before = self.archive.read_bytes()
        with patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}):
            code, out, err = self.invoke("--log-file", self.root / "recover.log")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.archive.read_bytes(), before)
        recovered = list(self.output.rglob("*.jpg"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), b"edited")
        self.assertIn("-photo-edited(1).jpg", recovered[0].name)
        metadata = json.loads(Path(str(recovered[0]) + ".JSON").read_text())
        for field, expected in {
            "dateTaken": "2023-01-01T00:00:00Z",
            "description": "Recovered edit",
            "latitude": 51.5,
            "longitude": -0.12,
            "favorited": True,
            "archived": True,
        }.items():
            self.assertEqual(metadata[field], expected)
        self.assertEqual({a["title"] for a in metadata["albums"]}, {"Trip", "Family"})
        from immich_ctl.folder_validate import inspect_folder

        files, warnings = inspect_folder(self.output)
        self.assertEqual(warnings, [])
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0][1], metadata)
        report = json.loads(self.manifest.read_text())
        self.assertEqual(report["files"][0]["sha256"], hashlib.sha256(b"edited").hexdigest())
        self.assertEqual(
            report["files"][0]["recovered_paths"], [str(recovered[0].relative_to(self.output))]
        )

        config = self.root / "empty.json"
        config.write_text("{}")
        with patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}):
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
                    str(self.root / "roundtrip.log"),
                    "--",
                    str(self.output),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        roundtrip = list((self.root / "roundtrip").rglob("*.JSON"))
        self.assertEqual(len(roundtrip), 1)
        restored = json.loads(roundtrip[0].read_text())
        for field in (
            "dateTaken",
            "description",
            "latitude",
            "longitude",
            "favorited",
            "archived",
            "albums",
        ):
            self.assertEqual(restored[field], metadata[field])


class RecoveryProperties(unittest.TestCase):
    @staticmethod
    @settings(max_examples=15, deadline=None, derandomize=True)
    @given(st.integers(1, 30), st.binary(min_size=1, max_size=50), st.booleans())
    def test_repeated_parts_and_names_do_not_change_checksum_coverage(index, contents, covered):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extracted = root / "extracted"
            extracted.mkdir()
            if covered:
                (extracted / "renamed.jpg").write_bytes(contents)
            archive = root / "takeout.zip"
            write_zip(
                archive,
                {
                    f"Year/photo({index}).jpg": b"original",
                    f"Year/photo.jpg.supplemental-metadata({index}).json": sidecar("photo.jpg"),
                    f"Year/photo-edited({index}).jpg": contents,
                },
            )
            outputs = []
            for paths in ([archive], [archive, archive]):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    assert (
                        recover(
                            paths,
                            extracted,
                            root / "output",
                            (date(2023, 1, 1), date(2023, 12, 31)),
                            lambda *_: 99,
                            dry_run=True,
                        )
                        == 0
                    )
                outputs.append(stdout.getvalue())
            assert outputs[0] == outputs[1]
            assert ("recover: 0; covered: 1" if covered else "recover: 1; covered: 0") in outputs[0]
            assert not (root / "output").exists()
