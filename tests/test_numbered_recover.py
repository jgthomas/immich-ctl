"""Independent recovery of ordinary numbered media, including extensionless titles."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.takeout_reconcile import reconcile_report
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

takeout = load_script("immich-takeout")


class NumberedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "takeout.zip"
        self.extracted = self.root / "normal"
        self.extracted.mkdir()
        self.edits = self.root / "edits"
        self.edits.mkdir()
        self.output = self.root / "numbered"
        self.manifest = self.root / "numbered.manifest.json"
        self.entries = {}
        for stem, extension in (("image", "jpg"), ("animation", "gif"), ("picture", "png")):
            self.entries[f"Photos from 2023/{stem}(1).{extension}"] = stem.encode()
            self.entries[f"Photos from 2023/{stem}.supplemental-metadata(1).json"] = sidecar(
                stem, stem
            )
        self.entries["Photos from 2023/image-edited(1).jpg"] = b"edit"
        write_zip(self.archive, self.entries)

    def invoke(self, *extra, action="recover-numbered"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = takeout.main(
                [
                    action,
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

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_dry_run_lists_only_numbered_non_edits_without_writes_or_tool(self):
        before = self.snapshot()
        with patch.object(takeout, "run_immich_go", side_effect=AssertionError("no tool")):
            code, out, err = self.invoke("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Numbered-file recovery", out)
        self.assertIn("recover: 3; covered: 0; unresolved: 0", out)
        self.assertNotIn("image-edited(1).jpg", out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        code, out, err = self.invoke("--dry-run", action="recover")
        self.assertEqual(code, 0, err)
        self.assertIn("recover: 1; covered: 0", out)
        self.assertNotIn('"kind": "numbered_media"', out)

    def test_byte_coverage_checks_every_supplied_folder_regardless_of_name(self):
        (self.extracted / "renamed.jpg").write_bytes(b"image")
        (self.edits / "different-name.gif").write_bytes(b"animation")
        code, out, err = self.invoke("--dry-run", "--extracted", self.edits)
        self.assertEqual(code, 0, err)
        self.assertIn("recover: 1; covered: 2", out)
        self.assertEqual(sum('"status": "recover"' in line for line in out.splitlines()), 1)

    def test_ambiguous_missing_undated_and_conflicting_metadata_stay_unresolved(self):
        key = "Photos from 2023/image.supplemental-metadata(1).json"
        original = dict(self.entries)
        variants = [
            {key: sidecar("image", "image", "0")},
            {key: b"{"},
            {"Photos from 2023/image(1).png": b"another image"},
            {
                "Album/metadata.json": {"title": "Album"},
                "Album/image(1).jpg": b"image",
                "Album/image.supplemental-metadata(1).json": sidecar("image", "image")
                | {"description": "conflict"},
            },
        ]
        for variant in variants:
            with self.subTest(variant=list(variant)):
                write_zip(self.archive, original | variant)
                code, out, err = self.invoke("--dry-run")
                self.assertEqual(code, 0, err)
                rows = [
                    json.loads(line) for line in out.splitlines() if line.startswith('{"path":')
                ]
                self.assertTrue(
                    all(r["status"] == "unresolved" for r in rows if "/image" in r["path"])
                )
                self.assertTrue(
                    any(r["status"] == "recover" for r in rows if "picture" in r["path"])
                )
        missing = dict(original)
        del missing[key]
        write_zip(self.archive, missing)
        self.assertIn(
            '"reason": "No unambiguous own metadata and capture date"', self.invoke("--dry-run")[1]
        )

    def test_filters_and_inclusive_final_day_apply_independently(self):
        self.entries["Photos from 2023/image.supplemental-metadata(1).json"]["trashed"] = True
        self.entries["Photos from 2023/animation.supplemental-metadata(1).json"][
            "googlePhotosOrigin"
        ] = {"fromPartnerSharing": {}}
        key = "Photos from 2023/picture.supplemental-metadata(1).json"
        for timestamp, expected in (("1704067199", "recover: 1"), ("1704067200", "recover: 0")):
            with self.subTest(timestamp=timestamp):
                self.entries[key] = sidecar("picture", "picture", timestamp)
                write_zip(self.archive, self.entries)
                code, out, err = self.invoke("--dry-run")
                self.assertEqual(code, 0, err)
                self.assertIn(expected, out)
                self.assertIn("excluded: 2", out)
        self.assertIn("recover: 1", self.invoke("--dry-run", "--include-partner")[1])

    def test_secondary_folder_output_and_log_guards_precede_writes(self):
        for args in (
            ["--extracted", self.edits, "--output", self.edits / "nested"],
            ["--extracted", self.edits, "--log-file", self.edits / "new.log"],
            ["--log-file", self.manifest],
            ["--extracted", self.root / "absent"],
        ):
            with self.subTest(args=args):
                before = self.snapshot()
                with patch.object(takeout, "run_immich_go") as tool:
                    code, _, _ = self.invoke(*args)
                self.assertEqual(code, 1)
                tool.assert_not_called()
                self.assertEqual(self.snapshot(), before)
        alias = self.edits / "link.jpg"
        alias.symlink_to(self.archive)
        self.assertEqual(self.invoke("--dry-run", "--extracted", self.edits)[0], 1)

    def fake_extract(self, args, paths):
        args.output.mkdir()
        with zipfile.ZipFile(paths[0]) as archive:
            for entry in archive.infolist():
                if not entry.filename.endswith((".jpg", ".gif", ".png")):
                    continue
                data = json.loads(archive.read(entry.filename + ".json"))
                target = args.output / Path(entry.filename).name
                target.write_bytes(archive.read(entry))
                Path(str(target) + ".JSON").write_text(
                    json.dumps(
                        {
                            "software": "immich-go version:0.32.0",
                            "fileName": target.name,
                            "dateTaken": datetime.fromtimestamp(
                                int(data["photoTakenTime"]["timestamp"]), UTC
                            ).isoformat(),
                        }
                    )
                )
        return 0

    def test_verified_publication_manifest_and_no_source_changes(self):
        before = self.snapshot()
        with patch.object(takeout, "run_immich_go", side_effect=self.fake_extract):
            code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        report = json.loads(self.manifest.read_text())
        self.assertEqual(report["recovery_kind"], "numbered_media")
        self.assertEqual(report["extracted_folders"], [str(self.extracted)])
        self.assertEqual(len(report["files"]), 3)
        for row in report["files"]:
            self.assertEqual(row["status"], "recover")
            target = self.output / row["recovered_paths"][0]
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), row["sha256"])
            self.assertEqual(
                json.loads(Path(str(target) + ".JSON").read_text())["fileName"], target.name
            )
        for path, payload in before.items():
            self.assertEqual(Path(path).read_bytes(), payload)
        self.assertFalse(list(self.root.glob(".immich-recovery-*")))

    def test_failed_or_incomplete_extraction_does_not_publish(self):
        def incomplete(args, paths):
            self.fake_extract(args, paths)
            next(args.output.glob("*.gif")).unlink()
            return 0

        for behavior in ({"return_value": 9}, {"side_effect": incomplete}):
            with self.subTest(behavior=str(behavior)):
                with patch.object(takeout, "run_immich_go", **behavior):
                    code, _, _ = self.invoke()
                self.assertNotEqual(code, 0)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.manifest.exists())

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_recovery_preserves_formats_dates_metadata_and_albums(self):
        source = self.entries["Photos from 2023/image.supplemental-metadata(1).json"]
        source.update(
            {
                "description": "Shared image",
                "geoData": {"latitude": 51.5, "longitude": -0.12},
                "favorited": True,
            }
        )
        self.entries.update(
            {
                "Album/metadata.json": {"title": "Library"},
                "Album/image(1).jpg": b"image",
                "Album/image.supplemental-metadata(1).json": source,
            }
        )
        write_zip(self.archive, self.entries)
        before = self.archive.read_bytes()
        with patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}):
            code, out, err = self.invoke("--log-file", self.root / "recover.log")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.archive.read_bytes(), before)
        report = json.loads(self.manifest.read_text())
        self.assertEqual(len({r["sha256"] for r in report["files"]}), 3)
        media = [p for p in self.output.rglob("*") if p.suffix in {".jpg", ".gif", ".png"}]
        self.assertEqual(len(media), 3)
        image = next(p for p in media if p.suffix == ".jpg")
        data = json.loads(Path(str(image) + ".JSON").read_text())
        self.assertEqual(data["description"], "Shared image")
        self.assertEqual(data["latitude"], 51.5)
        self.assertTrue(data["favorited"])
        self.assertEqual(data["albums"], [{"title": "Library"}])
        self.assertTrue(
            all(
                json.loads(Path(str(p) + ".JSON").read_text())["dateTaken"]
                == "2023-01-01T00:00:00Z"
                for p in media
            )
        )
        selection = (datetime(2023, 1, 1).date(), datetime(2023, 12, 31).date())
        reconciled = reconcile_report([self.archive], [self.output], selection)
        self.assertEqual(
            reconciled["selected"]["content_missing"], 1
        )  # Edited recovery remains separate.
        self.assertTrue(
            all(r["metadata_verified"] for r in reconciled["files"] if r["association"] == "direct")
        )

    @staticmethod
    @settings(max_examples=15, deadline=None, derandomize=True)
    @given(
        st.binary(min_size=1, max_size=50), st.integers(min_value=1, max_value=99), st.booleans()
    )
    def test_coverage_uses_bytes_not_numbering_or_existing_filename(content, number, present):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, folder = root / "export.zip", root / "existing"
            folder.mkdir()
            if present:
                (folder / "unrelated-name.gif").write_bytes(content)
            write_zip(
                archive,
                {
                    f"Year/image({number}).gif": content,
                    f"Year/image.supplemental-metadata({number}).json": sidecar("image", "item"),
                },
            )
            out = io.StringIO()
            with (
                redirect_stdout(out),
                patch.object(takeout, "run_immich_go", side_effect=AssertionError("no tool")),
            ):
                code = takeout.main(
                    [
                        "recover-numbered",
                        "--date-range",
                        "2023",
                        "--extracted",
                        str(folder),
                        "--output",
                        str(root / "output"),
                        "--dry-run",
                        str(archive),
                    ]
                )
            assert code == 0
            assert (
                "recover: 0; covered: 1" if present else "recover: 1; covered: 0"
            ) in out.getvalue()
            assert not (root / "output").exists()
