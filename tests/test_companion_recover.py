"""Read-only planning, real probing, safe publication and metadata policy tests."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import companion_recover as cr
from immich_ctl.takeout_recover import RecoveryError
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

IDENTIFIER = "6CB67428-FE2C-47E0-9E25-1C371EA63AEB"
OTHER = "25525016-48EC-45AE-87B6-8DA3CBCBD5C3"
DATE = "2025-06-15T12:00:00+00:00"
INFO = {"identifier": IDENTIFIER, "capture_time": DATE, "coordinates": [51.5, 0.0]}
takeout = load_script("immich-takeout")


def native(**extra):
    return {
        "software": cr.MARKER,
        "fileName": "photo.HEIC",
        "dateTaken": DATE,
        "latitude": 51.5,
        "longitude": 0.0,
        **extra,
    }


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "extracted"
        self.folder.mkdir()
        self.photo = self.folder / "photo.HEIC"
        self.photo.write_bytes(b"still " + IDENTIFIER.encode())
        self.metadata(native(albums=[{"title": "Trip", "description": "Narrative"}]))
        self.archive = self.root / "takeout.zip"
        self.entries = {"Photos from 2025/photo": b"video"}
        self.write()
        self.output = self.root / "recovered"
        self.manifest = self.root / "recovered.manifest.json"
        self.selection = (date(2025, 1, 1), date(2025, 12, 31))

    def metadata(self, data, path=None):
        p = path or self.photo
        p.with_name(p.name + ".JSON").write_text(json.dumps(data))

    def write(self, archive=None, entries=None):
        write_zip(archive or self.archive, self.entries if entries is None else entries)

    def run_recovery(self, **options):
        with (
            patch.object(cr.shutil, "which", return_value="ffprobe"),
            patch.object(cr, "probe", return_value=INFO),
        ):
            return cr.recover_companions(
                [self.archive], [self.folder], self.output, self.selection, **options
            )

    def test_dry_run_is_read_only_and_inclusive_dates_are_utc(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.selection = (date(2025, 6, 15), date(2025, 6, 15))
        report = self.run_recovery(dry_run=True)
        self.assertEqual(report["counts"], {"recover": 1})
        row = report["files"][0]
        self.assertEqual(row["association"], "shared_identifier")
        self.assertEqual(row["matched_stills"], [str(self.photo)])
        self.assertEqual(row["metadata"]["longitude"], 0)
        self.assertEqual(row["metadata"]["albums"], [{"title": "Trip", "description": "Narrative"}])
        self.assertEqual(row["metadata"]["fileName"], row["output_name"])
        self.assertTrue(row["output_name"].endswith("photo.MOV"))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_actual_recovery_round_trips_bytes_sidecar_and_manifest(self):
        before = (
            self.archive.read_bytes(),
            self.photo.read_bytes(),
            self.photo.with_suffix(".HEIC.JSON").read_bytes(),
        )
        r = self.run_recovery()
        row = r["files"][0]
        self.assertEqual((self.output / row["output_name"]).read_bytes(), b"video")
        self.assertEqual(
            json.loads((self.output / (row["output_name"] + ".JSON")).read_text()), row["metadata"]
        )
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(b"video").hexdigest())
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            before,
            (
                self.archive.read_bytes(),
                self.photo.read_bytes(),
                self.photo.with_suffix(".HEIC.JSON").read_bytes(),
            ),
        )
        self.assertFalse(list(self.root.glob(".immich-companions-*")))

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_recovered_sidecar_survives_real_immich_go_folder_reader(self):
        self.run_recovery()
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
                str(self.output),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        sidecars = list((self.root / "roundtrip").rglob("*.JSON"))
        self.assertEqual(len(sidecars), 1)
        metadata = json.loads(sidecars[0].read_text())
        self.assertEqual(metadata["latitude"], 51.5)
        self.assertEqual(metadata["dateTaken"], "2025-06-15T12:00:00Z")
        self.assertEqual(metadata["albums"], [{"title": "Trip", "description": "Narrative"}])
        self.assertEqual(sidecars[0].with_suffix("").read_bytes(), b"video")

    def test_missing_identifiers_are_not_guessed_and_standalone_does_not_inherit_albums(self):
        for identifier in (None, OTHER):
            with (
                self.subTest(identifier=identifier),
                patch.object(cr, "probe", return_value={**INFO, "identifier": identifier}),
                patch.object(cr.shutil, "which", return_value="ffprobe"),
            ):
                r = cr.recover_companions(
                    [self.archive], [self.folder], self.output, self.selection, dry_run=True
                )
                self.assertEqual(r["counts"], {"unresolved": 1})
                r = cr.recover_companions(
                    [self.archive],
                    [self.folder],
                    self.output,
                    self.selection,
                    dry_run=True,
                    include_unpaired=True,
                )
                m = r["files"][0]["metadata"]
                self.assertNotIn("albums", m)
                self.assertEqual(m["dateTaken"], DATE)
                self.assertEqual(m["latitude"], 51.5)
                self.assertEqual(m["longitude"], 0)
                self.assertEqual(r["files"][0]["association"], "embedded_metadata_only")

    def test_absent_embedded_dates_can_use_confirmed_still_but_not_standalone(self):
        metadata, refs, _ = cr.metadata_for(
            {**INFO, "capture_time": None},
            [{"path": "still", "identifiers": {IDENTIFIER}, "metadata": native()}],
            False,
        )
        self.assertEqual(metadata["dateTaken"], DATE)
        self.assertEqual(refs, ["still"])
        with self.assertRaisesRegex(ValueError, "embedded capture date"):
            cr.metadata_for({**INFO, "identifier": None, "capture_time": None}, [], True)

    def test_conflicting_date_or_gps_cannot_be_bypassed_by_standalone_option(self):
        refs = [{"path": "still", "identifiers": {IDENTIFIER}, "metadata": native()}]
        for info in (
            {**INFO, "capture_time": "2025-06-16T12:00:00Z"},
            {**INFO, "coordinates": [52.5, 0]},
            {**INFO, "coordinates": [51.5, 0.1]},
        ):
            with self.subTest(info=info), self.assertRaises(ValueError):
                cr.metadata_for(info, refs, True)

    def test_matching_still_copies_combine_albums_but_conflicts_are_unresolved(self):
        refs = [
            {"metadata": native(albums=[{"title": "Trip"}])},
            {"metadata": native(fileName="copy.HEIC", albums=[{"title": "Family"}])},
        ]
        self.assertEqual(
            cr.merged_metadata(refs)["albums"], [{"title": "Family"}, {"title": "Trip"}]
        )
        refs[1]["metadata"]["description"] = "conflict"
        with self.assertRaisesRegex(ValueError, "conflicting metadata"):
            cr.merged_metadata(refs)
        refs[1]["metadata"] = native(albums=[{"title": "Trip", "description": "different"}])
        with self.assertRaisesRegex(ValueError, "Conflicting album"):
            cr.merged_metadata(refs)

    def test_same_path_size_and_crc_do_not_discard_distinct_media_bytes(self):
        # These distinct eight-byte strings have the same CRC32 (0x4ddb0c25).
        self.entries = {"Photos from 2025/photo.MP4": b"plumless"}
        self.write()
        extra = self.root / "part2.zip"
        self.write(extra, {"Photos from 2025/photo.MP4": b"buckeroo"})
        with (
            patch.object(cr.shutil, "which", return_value="ffprobe"),
            patch.object(cr, "probe", return_value=INFO),
        ):
            report = cr.recover_companions(
                [self.archive, extra], [self.folder], self.output, self.selection
            )
        self.assertEqual(report["counts"], {"recover": 2})
        self.assertEqual(
            {p.read_bytes() for p in self.output.iterdir() if p.suffix != ".JSON"},
            {b"plumless", b"buckeroo"},
        )

    def test_coverage_checks_all_extracted_folders_by_bytes_not_names(self):
        second = self.root / "other"
        second.mkdir()
        video = second / "renamed.MP4"
        video.write_bytes(b"video")
        self.metadata(native(fileName=video.name), video)
        with (
            patch.object(cr.shutil, "which", return_value="ffprobe"),
            patch.object(cr, "probe") as probe,
        ):
            r = cr.recover_companions(
                [self.archive], [self.folder, second], self.output, self.selection
            )
        self.assertEqual(r["counts"], {"covered": 1})
        probe.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())

    def test_own_invalid_or_ambiguous_metadata_does_not_get_replaced(self):
        for data in (sidecar("photo.MP4"), b"{invalid"):
            with self.subTest(data=data):
                self.entries = {
                    "Photos from 2025/photo.MP4": b"video",
                    "Photos from 2025/photo.MP4.json": data,
                }
                self.write()
                r = self.run_recovery(dry_run=True, include_unpaired=True)
                self.assertEqual(r["counts"], {"own_sidecar": 1})

    def test_repeated_split_parts_and_duplicate_content_publish_one_album_union(self):
        extra = self.root / "part2.zip"
        self.write(extra, {"Trip/photo.MP4": b"video"})
        with (
            patch.object(cr.shutil, "which", return_value="ffprobe"),
            patch.object(cr, "probe", return_value=INFO),
        ):
            r = cr.recover_companions(
                [self.archive, extra, self.archive],
                [self.folder, self.folder],
                self.output,
                self.selection,
            )
        self.assertEqual(r["counts"], {"recover": 1, "duplicate_source": 1})
        self.assertEqual(len(list(self.output.iterdir())), 2)
        first = next(row for row in r["files"] if row["status"] == "recover")
        duplicate = next(row for row in r["files"] if row["status"] == "duplicate_source")
        self.assertEqual(duplicate["output_name"], first["output_name"])

    def test_unrelated_names_are_reported_and_only_explicit_standalone_scans_them(self):
        self.entries = {"Photos from 2025/unrelated.MP4": b"other"}
        self.write()
        r = self.run_recovery(dry_run=True)
        self.assertEqual(r["counts"], {"unrelated": 1})
        r = self.run_recovery(dry_run=True, include_unpaired=True)
        # Matching is identifier-based once explicitly scanned, despite another filename.
        self.assertEqual(r["counts"], {"recover": 1})

    def test_outside_dates_no_output_and_probe_failures_are_reported(self):
        self.selection = (date(2024, 1, 1), date(2024, 12, 31))
        self.assertEqual(self.run_recovery()["counts"], {"outside_dates": 1})
        self.assertFalse(self.output.exists())
        with (
            patch.object(cr.shutil, "which", return_value="ffprobe"),
            patch.object(cr, "probe", side_effect=ValueError("bad video")),
        ):
            r = cr.recover_companions([self.archive], [self.folder], self.output, self.selection)
        self.assertEqual(r["files"][0]["reason"], "bad video")
        self.assertFalse(self.output.exists())

    def test_existing_nested_source_and_symlink_destinations_are_rejected(self):
        for output in (self.folder / "nested", self.root, self.archive, self.output):
            if output == self.output:
                output.symlink_to(self.root / "missing")
            with self.subTest(output=output), self.assertRaises(RecoveryError):
                cr.recover_companions(
                    [self.archive], [self.folder], output, self.selection, dry_run=True
                )
        self.output.unlink()
        self.manifest.write_text("existing")
        with self.assertRaises(RecoveryError):
            self.run_recovery(dry_run=True)
        self.assertEqual(self.manifest.read_text(), "existing")

    def test_symlink_and_missing_native_sidecars_in_extracted_folder_fail(self):
        self.photo.with_suffix(".HEIC.JSON").unlink()
        with self.assertRaises(RecoveryError):
            self.run_recovery(dry_run=True)
        self.metadata({"fileName": "photo.HEIC"})
        with self.assertRaisesRegex(RecoveryError, "native sidecar"):
            self.run_recovery(dry_run=True)
        self.metadata(native())
        (self.folder / "link.HEIC").symlink_to(self.photo)
        with self.assertRaises(RecoveryError):
            self.run_recovery(dry_run=True)

    def test_publication_failure_or_source_change_leaves_no_output_or_manifest(self):
        r = self.run_recovery(dry_run=True)
        row = r["files"][0]
        self.write(entries={"Photos from 2025/photo": b"changed"})
        with self.assertRaisesRegex(RecoveryError, "changed"):
            cr.publish([row], self.output, self.manifest, r)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        self.write()
        with (
            patch.object(cr.json, "dump", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            cr.publish([row], self.output, self.manifest, r)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        self.assertFalse(list(self.root.glob(".immich-companions-*")))

    def test_cli_json_and_missing_probe_dependency(self):
        args = [
            "recover-companions",
            "--date-range",
            "2025",
            "--extracted",
            str(self.folder),
            "--output",
            str(self.output),
            "--dry-run",
            "--json",
            str(self.archive),
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            patch.object(cr.shutil, "which", return_value="ffprobe"),
            patch.object(cr, "probe", return_value=INFO),
        ):
            self.assertEqual(takeout.main(args), 0)
        self.assertEqual(json.loads(stdout.getvalue())["counts"], {"recover": 1})
        with (
            patch.object(cr.shutil, "which", return_value=None),
            self.assertRaisesRegex(RecoveryError, "ffprobe"),
        ):
            cr.recover_companions([self.archive], [self.folder], self.output, self.selection)

    def test_numbered_names_and_skipped_entries_do_not_hide_later_candidates(self):
        video = self.folder / "covered.MP4"
        video.write_bytes(b"covered")
        self.metadata(native(fileName=video.name), video)
        self.entries = {
            "notes.txt": b"notes",
            "empty/": b"",
            "Photos from 2025/other.txt": b"other",
            "Photos from 2025/unrelated.MP4": b"other",
            "Photos from 2025/own.MP4": b"own",
            "Photos from 2025/own.MP4.json": sidecar("own.MP4"),
            "Photos from 2025/photo(9).MP4": b"covered",
            "Photos from 2025/photo(1).MP4": b"first",
            "Photos from 2025/photo~2.MP4": b"second",
        }
        self.write()
        r = self.run_recovery(dry_run=True)
        self.assertEqual(
            r["counts"], {"unrelated": 1, "own_sidecar": 1, "covered": 1, "recover": 2}
        )
        self.assertEqual(
            {x["path"] for x in r["files"] if x["status"] == "recover"},
            {"Photos from 2025/photo(1).MP4", "Photos from 2025/photo~2.MP4"},
        )
        extra = self.root / "repeat.zip"
        self.write(extra, {"Photos from 2025/photo(1).MP4": b"first"})
        with (
            patch.object(cr.shutil, "which", return_value="ffprobe"),
            patch.object(cr, "probe", return_value=INFO),
        ):
            repeated = cr.recover_companions(
                [extra, self.archive], [self.folder], self.output, self.selection, dry_run=True
            )
        self.assertEqual(repeated["counts"], r["counts"])

    def test_unsafe_names_and_missing_or_symlink_parents_are_rejected(self):
        renamed = self.folder / "photo[1].HEIC"
        self.photo.rename(renamed)
        self.photo.with_name(self.photo.name + ".JSON").rename(
            renamed.with_name(renamed.name + ".JSON")
        )
        with self.assertRaises(RecoveryError):
            self.run_recovery(dry_run=True)
        with self.assertRaises(RecoveryError):
            cr.destinations([self.archive], [self.folder], self.root / "missing" / "output")
        link = self.root / "linked"
        link.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(RecoveryError):
            cr.destinations([self.archive], [], link / "output")

    def test_hash_verification_and_racing_output_prevent_publication(self):
        report = self.run_recovery(dry_run=True)
        row = report["files"][0]
        with self.assertRaises(RecoveryError):
            cr.publish([{**row, "sha256": "0" * 64}], self.output, self.manifest, report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        original = json.dump

        def race(*args, **kwargs):
            original(*args, **kwargs)
            self.output.mkdir()

        with patch.object(cr.json, "dump", side_effect=race), self.assertRaises(RecoveryError):
            cr.publish([row], self.output, self.manifest, report)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(self.manifest.exists())
        self.assertFalse(list(self.root.glob(".immich-companions-*")))

    def test_corrupt_member_is_never_published(self):
        report = self.run_recovery(dry_run=True)
        row = report["files"][0]
        with zipfile.ZipFile(self.archive) as z:
            entry = z.infolist()[0]
        payload = bytearray(self.archive.read_bytes())
        payload[entry.header_offset + 30 + len(entry.filename.encode())] ^= 1
        self.archive.write_bytes(payload)
        with self.assertRaises(zipfile.BadZipFile):
            cr.publish([row], self.output, self.manifest, report)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        self.assertFalse(list(self.root.glob(".immich-companions-*")))

    def test_metadata_validation_and_native_marker(self):
        valid = native(
            latitude=90, longitude=-180, favorited=False, archived=True, description="Caption"
        )
        result = cr.merged_metadata([{"metadata": valid}])
        for key in ("latitude", "longitude", "favorited", "archived", "description"):
            self.assertEqual(result[key], valid[key])
        self.assertEqual(result["software"], cr.MARKER)
        for field, value in [
            ("latitude", 90.1),
            ("longitude", -180.1),
            ("latitude", True),
            ("longitude", float("nan")),
            ("latitude", float("inf")),
            ("favorited", 1),
            ("archived", "false"),
            ("description", None),
            ("albums", "Trip"),
            ("albums", [{"title": 1}]),
            ("albums", [{"title": ""}]),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                cr.merged_metadata([{"metadata": native(**{field: value})}])
        self.assertEqual(
            cr.merged_metadata(
                [{"metadata": native(software="immich-go version:0.31.0")}, {"metadata": native()}]
            )["software"],
            cr.MARKER,
        )

    def test_capture_tolerance_gps_fallback_and_standalone_marker(self):
        refs = [{"path": "still", "identifiers": {IDENTIFIER}, "metadata": native()}]
        m, _, _ = cr.metadata_for({**INFO, "capture_time": "2025-06-15T12:00:02Z"}, refs, False)
        self.assertEqual(m["dateTaken"], DATE)
        with self.assertRaises(ValueError):
            cr.metadata_for({**INFO, "capture_time": "2025-06-15T12:00:03Z"}, refs, False)
        refs[0]["metadata"] = {"software": cr.MARKER, "fileName": "still", "dateTaken": DATE}
        m, _, _ = cr.metadata_for(INFO, refs, False)
        self.assertEqual((m["latitude"], m["longitude"]), (51.5, 0))
        m, _, _ = cr.metadata_for({**INFO, "identifier": None, "coordinates": None}, [], True)
        self.assertEqual(m, {"software": cr.MARKER, "dateTaken": DATE})

    @unittest.skipUnless(hasattr(time, "tzset"), "requires process timezone support")
    def test_selection_is_utc_in_a_non_utc_desktop_timezone(self):
        self.metadata(native(dateTaken="2025-06-15T23:30:00Z"))
        self.selection = (date(2025, 6, 15), date(2025, 6, 15))
        try:
            with patch.dict(os.environ, {"TZ": "Pacific/Kiritimati"}):
                time.tzset()
                with (
                    patch.object(cr.shutil, "which", return_value="ffprobe"),
                    patch.object(
                        cr,
                        "probe",
                        return_value={**INFO, "capture_time": "2025-06-16T01:30:00+02:00"},
                    ),
                ):
                    r = cr.recover_companions(
                        [self.archive], [self.folder], self.output, self.selection, dry_run=True
                    )
                self.assertEqual(r["counts"], {"recover": 1})
        finally:
            time.tzset()

    def test_staging_works_when_system_temp_and_output_are_on_different_filesystems(self):
        other = self.root / "system-temp"
        other.mkdir()
        real_rename = os.rename

        def different_filesystems(source, target, *args, **kwargs):
            if Path(source).parent == other:
                raise OSError(18, "Invalid cross-device link")
            return real_rename(source, target, *args, **kwargs)

        with (
            patch.object(cr.tempfile, "gettempdir", return_value=str(other)),
            patch.object(os, "rename", side_effect=different_filesystems),
        ):
            report = self.run_recovery()
        self.assertEqual(report["counts"], {"recover": 1})
        self.assertTrue(self.output.is_dir())
        self.assertFalse(list(other.iterdir()))


class ProbeTests(unittest.TestCase):
    def test_large_stills_are_scanned_with_bounded_working_buffers(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            photo = folder / "photo.HEIC"
            contents = b"still" + b"x" * (5 * 1024 * 1024) + IDENTIFIER.encode()
            photo.write_bytes(contents)
            photo.with_name(photo.name + ".JSON").write_text(json.dumps(native()))
            original = cr.UUID
            sizes = []

            def inspect(data):
                sizes.append(len(data))
                self.assertLess(len(data), 2 * 1024 * 1024)
                return original.findall(data)

            with patch.object(cr, "UUID", wraps=original) as pattern:
                pattern.findall.side_effect = inspect
                hashes, references = cr.local_files([folder])
            self.assertGreater(len(sizes), 1)
            self.assertIn(IDENTIFIER, references[0]["identifiers"])
            self.assertEqual(hashes, {hashlib.sha256(contents).hexdigest()})

    def test_probe_failure_audio_only_and_output_bounds(self):
        valid = {"streams": [{"codec_type": "video"}], "format": {"format_name": "mov"}}
        encoded = json.dumps(valid).encode()
        with (
            patch.object(
                cr.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)
            ),
            self.assertRaises(ValueError),
        ):
            cr.probe(Path("video"), "ffprobe")
        cases = [
            (1, encoded),
            (0, b"{invalid"),
            (0, b"[]"),
            (0, json.dumps({**valid, "streams": [{"codec_type": "audio"}]}).encode()),
            (0, json.dumps({**valid, "format": {"format_name": "avi"}}).encode()),
            (0, json.dumps({**valid, "format": None}).encode()),
            (0, json.dumps({**valid, "format": {"format_name": "mov", "tags": []}}).encode()),
            (0, encoded + b" " * (8 * 1024 * 1024 + 1 - len(encoded))),
        ]
        for code, payload in cases:
            with (
                self.subTest(code=code, size=len(payload)),
                patch.object(
                    cr.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], code, payload, b""),
                ),
                self.assertRaises(ValueError),
            ):
                cr.probe(Path("video"), "ffprobe")
        with patch.object(
            cr.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, encoded + b" " * (8 * 1024 * 1024 - len(encoded)), b""
            ),
        ):
            self.assertEqual(
                cr.probe(Path("video"), "ffprobe"),
                {"identifier": None, "capture_time": None, "coordinates": None},
            )

    def test_probe_parses_video_identity_timezone_and_zero_gps_and_refuses_network(self):
        value = {
            "streams": [{"codec_type": "video"}],
            "format": {
                "format_name": "mov,mp4",
                "tags": {
                    "com.apple.quicktime.content.identifier": IDENTIFIER.lower(),
                    "com.apple.quicktime.creationdate": "2025-06-15T13:00:00+0100",
                    "com.apple.quicktime.location.ISO6709": "+51.5000+000.0000+123.4/",
                },
            },
        }

        def run(command, **kwargs):
            self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file")
            self.assertEqual(command[command.index("-format_whitelist") + 1], "mov")
            self.assertEqual(kwargs["timeout"], 30)
            self.assertIs(kwargs["check"], False)
            return subprocess.CompletedProcess(command, 0, json.dumps(value).encode(), b"")

        with patch.object(cr.subprocess, "run", side_effect=run):
            self.assertEqual(cr.probe(Path("/tmp/video.bin"), "ffprobe"), INFO)
        for field, bad in (
            ("com.apple.quicktime.content.identifier", "bad"),
            ("com.apple.quicktime.location.ISO6709", "+91.0+000.0/"),
            ("com.apple.quicktime.location.ISO6709", "+51.0+181.0/"),
            ("com.apple.quicktime.location.ISO6709", "garbage"),
        ):
            with self.subTest(field=field, bad=bad):
                edited = json.loads(json.dumps(value))
                edited["format"]["tags"][field] = bad
                with (
                    patch.object(
                        cr.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(
                            [], 0, json.dumps(edited).encode(), b""
                        ),
                    ),
                    self.assertRaises(ValueError),
                ):
                    cr.probe(Path("video"), "ffprobe")

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "optional real FFmpeg probe integration"
    )
    def test_real_extensionless_video_recovery_with_embedded_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "extensionless"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=16x16:r=5",
                    "-t",
                    "0.4",
                    "-c:v",
                    "mpeg4",
                    "-metadata",
                    "com.apple.quicktime.content.identifier=" + IDENTIFIER,
                    "-metadata",
                    "com.apple.quicktime.creationdate=" + DATE,
                    "-metadata",
                    "com.apple.quicktime.location.ISO6709=+51.5000+000.0000/",
                    "-movflags",
                    "use_metadata_tags",
                    "-f",
                    "mov",
                    str(path),
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(cr.probe(path, shutil.which("ffprobe")), INFO)
            extracted = Path(temp) / "prepared"
            extracted.mkdir()
            still = extracted / "extensionless.HEIC"
            still.write_bytes(b"still " + IDENTIFIER.encode())
            still.with_name(still.name + ".JSON").write_text(
                json.dumps(native(fileName=still.name, albums=[{"title": "Trip"}]))
            )
            archive = Path(temp) / "part.zip"
            write_zip(archive, {"Photos from 2025/extensionless": path.read_bytes()})
            output = Path(temp) / "recovery"
            report = cr.recover_companions(
                [archive], [extracted], output, (date(2025, 1, 1), date(2025, 12, 31))
            )
            self.assertEqual(report["counts"], {"recover": 1})
            row = report["files"][0]
            self.assertEqual(row["embedded"], INFO)
            self.assertEqual((output / row["output_name"]).read_bytes(), path.read_bytes())
            self.assertEqual(
                json.loads((output / (row["output_name"] + ".JSON")).read_text())["albums"],
                [{"title": "Trip"}],
            )

    @staticmethod
    @given(st.binary(max_size=200))
    @settings(max_examples=30)
    def test_sha256_coverage_invariant_and_identifier_at_chunk_boundary(payload):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            p = folder / "photo.HEIC"
            contents = (
                b"still" + payload + b"x" * (1024 * 1024 - 15 - len(payload)) + IDENTIFIER.encode()
            )
            p.write_bytes(contents)
            p.with_name(p.name + ".JSON").write_text(json.dumps(native()))
            checksums, refs = cr.local_files([folder])
            assert checksums == {hashlib.sha256(contents).hexdigest()}
            assert IDENTIFIER in refs[0]["identifiers"]
