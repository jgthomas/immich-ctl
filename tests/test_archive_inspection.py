"""Invocation-scoped inspection reuse preserves reports and rejects stale summaries."""

import copy
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from immich_ctl import archive_inspection as shared
from immich_ctl.takeout import inspect_archives
from immich_ctl.takeout_audit import audit_report
from immich_ctl.takeout_upload_audit import archive_inputs, upload_audit
from tests import test_upload_audit as fixtures
from tests.test_takeout_audit import write_zip


class ArchiveInspectionTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.UploadAuditTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.entries.update(
            {
                "Photos from 2023/photo(1).jpg": b"numbered photo",
                "Photos from 2023/photo.jpg.supplemental-metadata(1).json": self.fx.data
                | {"url": "numbered"},
                "Trip A/metadata.json": {"title": "Trip"},
                "Trip A/photo.jpg": b"photo",
                "Trip A/photo.jpg.json": self.fx.data,
                "Trip B/metadata.json": {"title": "Trip"},
                "Trip B/photo.jpg": b"photo",
                "Trip B/photo.jpg.json": self.fx.data,
                "Photos from 2023/bad.jpg": b"uncertain media",
                "Photos from 2023/bad.jpg.json": b"malformed JSON",
            }
        )
        self.fx.write()

    def test_cli_numbered_and_server_reports_share_exactly_one_inspection(self):
        for folders in (False, True):
            with self.subTest(folders=folders):
                arguments = ["--json", "--date-range", "2023"]
                if folders:
                    arguments += ["--extracted", self.fx.folder]
                with patch.object(shared, "inspect_archives", wraps=inspect_archives) as inspect:
                    code, output, error = self.fx.invoke(*arguments)
                self.assertEqual(code, 0, error)
                inspect.assert_called_once_with([self.fx.archive.resolve()])
                report = json.loads(output)
                self.assertEqual(report["numbered_files"], 1)
                self.assertEqual(
                    report["source_album_title_collisions"],
                    [{"title": "Trip", "source_directories": ["Trip A", "Trip B"]}],
                )
                self.assertTrue(report["warnings"])
                self.assertEqual(
                    report["server_audit"]["input_kind"],
                    "extracted_folders" if folders else "takeout_candidates",
                )

    def test_standalone_upload_audit_also_inspects_only_once(self):
        with patch.object(shared, "inspect_archives", wraps=inspect_archives) as inspect:
            report = self.fx.report()
        inspect.assert_called_once()
        self.assertEqual(report["summary"]["source_album_title_collisions"], 1)
        self.assertTrue(report["has_warnings"])

    def test_shared_reports_equal_independent_reports_without_mutating_inspection(self):
        paths = [self.fx.archive]
        expected_numbered = audit_report(paths)
        expected_server = self.fx.report()
        expected_inputs = archive_inputs(paths, fixtures.SELECTION, False)
        session = shared.ArchiveInspection(paths)
        original = copy.deepcopy(session.get(paths))
        with patch.object(shared, "inspect_archives", side_effect=AssertionError("reinspection")):
            self.assertEqual(audit_report(paths, inspection=session), expected_numbered)
            self.assertEqual(
                upload_audit(paths, self.fx.client(), inspection=session), expected_server
            )
            self.assertEqual(
                archive_inputs(paths, fixtures.SELECTION, False, inspection=session),
                expected_inputs,
            )
        self.assertEqual(session.get(paths), original)

    def test_split_archives_and_repeated_parts_keep_cross_part_matches(self):
        media = self.fx.root / "media.zip"
        metadata = self.fx.root / "metadata.zip"
        write_zip(
            media,
            {name: value for name, value in self.fx.entries.items() if not name.endswith(".json")},
        )
        write_zip(
            metadata,
            {name: value for name, value in self.fx.entries.items() if name.endswith(".json")},
        )
        paths = [media, metadata, media]
        expected = inspect_archives(paths)
        session = shared.ArchiveInspection(paths)
        with patch.object(shared, "inspect_archives", wraps=inspect_archives) as inspect:
            self.assertEqual(session.get(paths), expected)
            server = upload_audit(paths, self.fx.client(), inspection=session)
            numbered = audit_report(paths, inspection=session)
        inspect.assert_called_once_with([media.resolve(), metadata.resolve()])
        self.assertEqual(numbered["files"][0]["association"], "direct")
        self.assertEqual(server["summary"]["source_album_title_collisions"], 1)

    def test_each_cli_invocation_reads_fresh_metadata(self):
        self.fx.asset["checksum"] = fixtures.encoded(b"photo")
        self.fx.asset["exifInfo"]["description"] = "desired"
        with patch.object(shared, "inspect_archives", wraps=inspect_archives) as inspect:
            code, first, error = self.fx.invoke("--json")
            self.assertEqual(code, 0, error)
            self.fx.data["description"] = "new source description"
            self.fx.write()
            code, second, error = self.fx.invoke("--json")
            self.assertEqual(code, 0, error)
        self.assertEqual(inspect.call_count, 2)
        before = json.loads(first)["server_audit"]["existing_checksum"]
        after = json.loads(second)["server_audit"]["existing_checksum"]
        self.assertTrue(before)
        self.assertTrue(all("description differs" not in row["differences"] for row in before))
        self.assertTrue(all("description differs" in row["differences"] for row in after))

    def test_numbered_only_command_inspects_once_and_has_no_server_calls(self):
        with (
            patch.object(shared, "inspect_archives", wraps=inspect_archives) as inspect,
            patch.object(
                fixtures.script, "load_config", side_effect=AssertionError("server access")
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = fixtures.script.main(["audit", "--json", str(self.fx.archive)])
        self.assertEqual(code, 0)
        inspect.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())["numbered_files"], 1)

    def test_changed_archive_rejects_previously_inspected_metadata(self):
        paths = [self.fx.archive]
        session = shared.ArchiveInspection(paths)
        session.get(paths)
        before = self.fx.archive.stat()
        self.fx.entries["Photos from 2023/new.jpg"] = b"new"
        self.fx.write()
        os.utime(self.fx.archive, ns=(before.st_atime_ns, before.st_mtime_ns))
        with (
            self.assertRaisesRegex(ValueError, "changed"),
            patch.object(
                shared, "inspect_archives", side_effect=AssertionError("stale inspection reused")
            ),
        ):
            session.get(paths)

    def test_changes_during_first_inspection_are_not_cached(self):
        paths = [self.fx.archive]
        session = shared.ArchiveInspection(paths)

        def inspect_and_change(paths):
            report = inspect_archives(paths)
            self.fx.entries["Photos from 2023/new.jpg"] = b"new"
            self.fx.write()
            return report

        with patch.object(shared, "inspect_archives", side_effect=inspect_and_change):
            with self.assertRaisesRegex(ValueError, "changed"):
                session.get(paths)
        self.assertEqual(session.get(paths), inspect_archives(paths))

    def test_failed_inspection_is_not_reused_and_construction_is_lazy(self):
        paths = [self.fx.archive]
        with patch.object(shared, "inspect_archives", side_effect=ValueError("failed")) as inspect:
            session = shared.ArchiveInspection(paths)
            inspect.assert_not_called()
            with self.assertRaisesRegex(ValueError, "failed"):
                session.get(paths)
        with patch.object(shared, "inspect_archives", wraps=inspect_archives) as inspect:
            session.get(paths)
            session.get(paths)
        inspect.assert_called_once()

    def test_mismatched_archive_set_or_order_is_rejected_before_inspection(self):
        second = self.fx.root / "other.zip"
        write_zip(second, {"Other/photo.jpg": b"other"})
        session = shared.ArchiveInspection([self.fx.archive, second])
        for paths in ([second], [self.fx.archive], [second, self.fx.archive]):
            with self.subTest(paths=paths), patch.object(shared, "inspect_archives") as inspect:
                with self.assertRaisesRegex(ValueError, "does not match"):
                    session.get(paths)
                inspect.assert_not_called()

    def test_equivalent_paths_and_duplicates_share_inspection(self):
        relative = Path(os.path.relpath(self.fx.archive))
        session = shared.ArchiveInspection([self.fx.archive, relative])
        with patch.object(shared, "inspect_archives", wraps=inspect_archives) as inspect:
            self.assertEqual(session.get([relative]), session.get([self.fx.archive]))
        inspect.assert_called_once_with([self.fx.archive.resolve()])
