"""Dated album repairs use source identity and preserve all other metadata."""

import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import album_repair
from immich_ctl.album_names import apply_names, dated_names
from immich_ctl.takeout import inspect_archives
from immich_ctl.takeout_reconcile import reconcile_report
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

script = load_script("immich-repair-album-names")
takeout = load_script("immich-takeout")
SELECTION = (date(2023, 1, 1), date(2023, 12, 31))


class AlbumRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "takeout.zip"
        self.folder = self.root / "extracted"
        self.folder.mkdir()
        self.mapping = self.root / "albums.json"
        self.entries = {}
        self.native = []
        for index, directory in enumerate(("First", "Second")):
            name = f"photo{index}.jpg"
            self.entries.update(
                {
                    directory + "/metadata.json": {
                        "title": "Trip",
                        "date": {"timestamp": str(1672531200 + index * 86400)},
                        "enrichments": [{"narrativeEnrichment": {"text": directory}}],
                    },
                    directory + "/" + name: directory.encode(),
                    directory + "/" + name + ".json": sidecar(name, directory),
                }
            )
            media = self.folder / name
            media.write_bytes(directory.encode())
            native = Path(str(media) + ".JSON")
            native.write_text(
                json.dumps(
                    {
                        "software": "immich-go version:0.32.0",
                        "fileName": name,
                        "dateTaken": "2023-01-01T00:00:00Z",
                        "custom": [False, "unchanged"],
                        "albums": [{"title": "Trip", "description": directory}],
                    }
                )
            )
            self.native.append(native)
        write_zip(self.archive, self.entries)

    def invoke(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = script.main(
                [
                    "--extracted",
                    str(self.folder),
                    "--mapping",
                    str(self.mapping),
                    *map(str, extra),
                    str(self.archive),
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_preview_repair_reconciliation_and_idempotence(self):
        before = self.snapshot()
        code, out, err = self.invoke("--dry-run", "--title", "Trip")
        self.assertEqual(code, 0, err)
        self.assertIn('"new_title": "Trip (2023-01-02)"', out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.mapping.exists())
        code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(self.mapping.read_text()),
            {
                "First": "Trip (2023-01-01)",
                "Second": "Trip (2023-01-02)",
            },
        )
        for index, native in enumerate(self.native):
            expected = json.loads(before[str(native)])
            expected["albums"][0]["title"] = f"Trip (2023-01-0{index + 1})"
            self.assertEqual(json.loads(native.read_text()), expected)
        self.assertEqual(self.archive.read_bytes(), before[str(self.archive)])
        report = reconcile_report([self.archive], [self.folder], SELECTION, album_map=self.mapping)
        self.assertTrue(report["metadata_and_albums_validated"])
        self.assertEqual(report["source_album_title_collisions"], [])
        after = self.snapshot()
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.snapshot(), after)
        out = io.StringIO()
        with redirect_stdout(out):
            code = takeout.main(
                [
                    "reconcile",
                    "--date-range",
                    "2023",
                    "--extracted",
                    str(self.folder),
                    "--album-map",
                    str(self.mapping),
                    "--json",
                    str(self.archive),
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["metadata_and_albums_validated"])

    def test_invalid_or_colliding_dates_and_stale_mapping_prevent_all_writes(self):
        original = copy.deepcopy(self.entries)
        for value in (
            None,
            {"timestamp": "0"},
            {"timestamp": "invalid"},
            {"timestamp": "1672531200"},
        ):
            with self.subTest(value=value):
                self.entries = copy.deepcopy(original)
                self.entries["Second/metadata.json"]["date"] = value
                write_zip(self.archive, self.entries)
                before = self.snapshot()
                self.assertEqual(self.invoke()[0], 1)
                self.assertEqual(self.snapshot(), before)
        write_zip(self.archive, original)
        self.mapping.write_text('{"First":"arbitrary"}')
        before = self.snapshot()
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.snapshot(), before)
        _, _, titles, _, _, _, coverage = inspect_archives([self.archive])
        with self.assertRaisesRegex(ValueError, "does not match"):
            apply_names(self.mapping, coverage, titles)

    def test_unknown_bytes_missing_memberships_or_changed_metadata_stop_entire_plan(self):
        original = self.native[1].read_bytes()
        for problem in ("bytes", "metadata", "missing", "extra"):
            with self.subTest(problem=problem):
                (self.folder / "photo1.jpg").write_bytes(b"Second")
                data = json.loads(original)
                if problem == "bytes":
                    (self.folder / "photo1.jpg").write_bytes(b"unknown")
                elif problem == "metadata":
                    data["albums"][0]["description"] = "wrong narrative"
                elif problem == "missing":
                    data["albums"] = []
                else:
                    data["albums"].append({"title": "Trip"})
                self.native[1].write_text(json.dumps(data))
                before = self.snapshot()
                code, _, _ = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(self.snapshot(), before)

    def test_mapping_inside_inputs_or_symlink_and_unusable_sidecars_are_rejected(self):
        for mapping in (self.folder / "map.json", self.archive):
            before = self.snapshot()
            self.assertEqual(self.invoke("--mapping", mapping)[0], 1)
            self.assertEqual(self.snapshot(), before)
        alias = self.root / "alias.json"
        alias.symlink_to(self.native[0])
        self.assertEqual(self.invoke("--mapping", alias)[0], 1)
        self.native[1].write_text('{"software":"immich-go version:0.32.0","albums":[],"albums":[]}')
        before = self.snapshot()
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.snapshot(), before)

    def test_selected_title_group_ignores_other_groups_and_preserves_source_metadata(self):
        for directory, timestamp in (("Third", "1672704000"), ("Fourth", "1672790400")):
            self.entries[directory + "/metadata.json"] = {
                "title": "Other",
                "date": {"timestamp": timestamp},
            }
        write_zip(self.archive, self.entries)
        code, _, err = self.invoke("--title", "Trip")
        self.assertEqual(code, 0, err)
        mapping = json.loads(self.mapping.read_text())
        self.assertEqual(set(mapping), {"First", "Second"})
        _, _, titles, _, _, _, coverage = inspect_archives([self.archive])
        source = copy.deepcopy(coverage.album_metadata)
        self.assertEqual(apply_names(self.mapping, coverage, titles), mapping)
        self.assertEqual(titles["Third"], "Other")
        self.assertEqual(titles["Fourth"], "Other")
        for directory, versions in source.items():
            expected = (
                [dict(versions[0], title=mapping[directory])] if directory in mapping else versions
            )
            self.assertEqual(coverage.album_metadata[directory], expected)
            self.assertEqual(titles[directory], expected[0]["title"])

    def test_dated_title_cannot_merge_with_an_existing_nonduplicate_album(self):
        self.entries["Existing/metadata.json"] = {"title": "Trip (2023-01-01)"}
        write_zip(self.archive, self.entries)
        before = self.snapshot()
        code, _, err = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("does not uniquely distinguish", err)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_and_conflicting_source_album_metadata_cannot_be_used(self):
        _, _, titles, _, _, _, coverage = inspect_archives([self.archive])
        original = copy.deepcopy(coverage)
        for kind in ("missing", "conflicting", "unreadable_copy"):
            with self.subTest(kind=kind):
                coverage = copy.deepcopy(original)
                if kind == "missing":
                    coverage.album_metadata.pop("First")
                elif kind == "conflicting":
                    coverage.album_metadata["First"].append(
                        dict(coverage.album_metadata["First"][0], description="conflict")
                    )
                else:
                    coverage.invalid_album_metadata.add("First")
                with self.assertRaises(ValueError):
                    dated_names(coverage, titles)

    def test_invalid_mapping_payloads_are_rejected_without_changing_expectations(self):
        valid = {"First": "Trip (2023-01-01)", "Second": "Trip (2023-01-02)"}
        duplicate = '{"First":"wrong","First":"Trip (2023-01-01)","Second":"Trip (2023-01-02)"}'
        for payload in (
            "{}",
            "[]",
            "null",
            json.dumps(valid | {"Unknown": "x"}),
            json.dumps(valid | {"First": 1}),
            duplicate,
        ):
            with self.subTest(payload=payload):
                self.mapping.write_text(payload)
                _, _, titles, _, _, _, coverage = inspect_archives([self.archive])
                before = copy.deepcopy((coverage, titles))
                with self.assertRaises(ValueError):
                    apply_names(self.mapping, coverage, titles)
                self.assertEqual((coverage, titles), before)

    def test_mapping_reads_are_bounded_and_oversized_payloads_are_rejected(self):
        payload = json.dumps({"First": "Trip (2023-01-01)", "Second": "Trip (2023-01-02)"})
        real_open = Path.open

        @contextmanager
        def bounded_open(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as stream:

                class Reader:
                    def read(reader, size=-1):
                        self.assertIsInstance(size, int)
                        self.assertGreater(size, 0)
                        self.assertLessEqual(size, 513)
                        return stream.read(size)

                yield Reader()

        for size in (512, 513):
            self.mapping.write_text(payload + " " * (size - len(payload)))
            _, _, titles, _, _, _, coverage = inspect_archives([self.archive])
            with (
                patch("immich_ctl.album_names.JSON_LIMIT", 512),
                patch.object(Path, "open", autospec=True, side_effect=bounded_open),
            ):
                if size == 512:
                    self.assertEqual(
                        apply_names(self.mapping, coverage, titles), json.loads(payload)
                    )
                else:
                    with self.assertRaises(ValueError):
                        apply_names(self.mapping, coverage, titles)

    def test_mapping_symlink_is_rejected_even_when_target_contains_a_valid_mapping(self):
        self.mapping.write_text(
            json.dumps(
                {
                    "First": "Trip (2023-01-01)",
                    "Second": "Trip (2023-01-02)",
                },
                indent=2,
            )
            + "\n"
        )
        alias = self.root / "mapping-alias.json"
        alias.symlink_to(self.mapping)
        before = self.snapshot()
        code, _, _ = self.invoke("--mapping", alias)
        self.assertEqual(code, 1)
        self.assertEqual(self.snapshot(), before)

    def test_native_sidecar_reads_are_bounded_and_oversized_inputs_make_no_changes(self):
        data = json.loads(self.native[1].read_text())
        data["albums"][0]["title"] = "Trip (2023-01-02)"
        payload = json.dumps(data)
        real_open = Path.open

        @contextmanager
        def bounded_open(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as stream:
                if path != self.native[1]:
                    yield stream
                    return

                class Reader:
                    def read(reader, size=-1):
                        # Folder inspection uses its normal 8 MiB bound first.
                        if size != 8 * 1024 * 1024 + 1:
                            self.assertIsInstance(size, int)
                            self.assertGreater(size, 0)
                            self.assertLessEqual(size, 513)
                        return stream.read(size)

                yield Reader()

        for size in (512, 513):
            self.native[1].write_text(payload + " " * (size - len(payload)))
            before = self.snapshot()
            with (
                patch("immich_ctl.album_repair.JSON_LIMIT", 512),
                patch.object(Path, "open", autospec=True, side_effect=bounded_open),
            ):
                code, _, err = self.invoke()
            self.assertEqual(code, 0 if size == 512 else 1, err)
            if size == 513:
                self.assertEqual(self.snapshot(), before)

    def test_unrelated_zip_entries_and_media_do_not_hide_later_repairs(self):
        self.entries = {"Takeout/Other/ignored.txt": b"ignore"} | self.entries
        write_zip(self.archive, self.entries)
        unrelated = self.folder / "000-unrelated.jpg"
        unrelated.write_bytes(b"unrelated")
        metadata = {"software": "immich-go version:0.32.0", "fileName": unrelated.name}
        Path(str(unrelated) + ".JSON").write_text(json.dumps(metadata))
        code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        for index, native in enumerate(self.native):
            self.assertEqual(
                json.loads(native.read_text())["albums"][0]["title"], f"Trip (2023-01-0{index + 1})"
            )
        self.assertEqual(json.loads(Path(str(unrelated) + ".JSON").read_text()), metadata)

    def test_partly_repaired_folders_finish_without_rewriting_completed_files(self):
        original = self.native[1].read_bytes()
        self.assertEqual(self.invoke()[0], 0)
        before = self.native[0].read_bytes()
        modified = self.native[0].stat().st_mtime_ns
        self.native[1].write_bytes(original)
        code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.native[0].read_bytes(), before)
        self.assertEqual(self.native[0].stat().st_mtime_ns, modified)
        self.assertEqual(
            json.loads(self.native[1].read_text())["albums"][0]["title"], "Trip (2023-01-02)"
        )

    def test_incompatible_or_ambiguous_native_metadata_stops_before_writes(self):
        original = self.native[1].read_text()
        data = json.loads(original)
        for kind in ("missing_marker", "duplicate_custom_key", "glob_name"):
            with self.subTest(kind=kind):
                self.native[1].write_text(original)
                if kind == "missing_marker":
                    changed = dict(data)
                    del changed["software"]
                    self.native[1].write_text(json.dumps(changed))
                elif kind == "duplicate_custom_key":
                    self.native[1].write_text(original[:-1] + ',"custom":"different"}')
                else:
                    media = self.folder / "photo1.jpg"
                    renamed = self.folder / "photo[1].jpg"
                    media.rename(renamed)
                    self.native[1].rename(Path(str(renamed) + ".JSON"))
                before = self.snapshot()
                self.assertEqual(self.invoke()[0], 1)
                self.assertEqual(self.snapshot(), before)

    def test_direct_repair_and_overlapping_folders_process_every_media_once(self):
        nested = self.folder / "child"
        nested.mkdir()
        first = self.folder / "photo0.jpg"
        first.rename(nested / first.name)
        self.native[0].rename(nested / self.native[0].name)
        later = self.root / "z-later"
        later.mkdir()
        shutil.copy2(self.folder / "photo1.jpg", later / "photo1.jpg")
        shutil.copy2(self.native[1], later / self.native[1].name)
        with redirect_stdout(io.StringIO()):
            album_repair.repair([self.archive], [self.folder, nested, later], self.mapping)
        for native in [nested / "photo0.jpg.JSON", self.native[1], later / self.native[1].name]:
            self.assertTrue(
                json.loads(native.read_text())["albums"][0]["title"].startswith("Trip (2023-")
            )

    def test_shared_content_keeps_both_source_album_memberships(self):
        for directory in ("First", "Second"):
            self.entries[directory + "/shared.jpg"] = b"shared"
            self.entries[directory + "/shared.jpg.json"] = sidecar("shared.jpg", "shared")
        write_zip(self.archive, self.entries)
        media = self.folder / "shared.jpg"
        media.write_bytes(b"shared")
        data = json.loads(self.native[0].read_text())
        data["fileName"] = "shared.jpg"
        data["albums"].append({"title": "Trip", "description": "Second"})
        native = Path(str(media) + ".JSON")
        native.write_text(json.dumps(data))
        native.chmod(0o640)
        code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        repaired = json.loads(native.read_text())
        self.assertEqual(
            repaired["albums"],
            [
                {"title": "Trip (2023-01-01)", "description": "First"},
                {"title": "Trip (2023-01-02)", "description": "Second"},
            ],
        )
        self.assertEqual(native.stat().st_mode & 0o777, 0o640)
        self.assertEqual(media.read_bytes(), b"shared")
        self.assertTrue(
            reconcile_report([self.archive], [self.folder], SELECTION, album_map=self.mapping)[
                "metadata_and_albums_validated"
            ]
        )

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_extraction_and_folder_reader_keep_dated_album_names(self):
        output = self.root / "native"
        with patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}):
            with redirect_stdout(io.StringIO()):
                code = takeout.main(
                    [
                        "extract",
                        "--date-range",
                        "2023",
                        "--output",
                        str(output),
                        "--log-file",
                        str(self.root / "extract.log"),
                        str(self.archive),
                    ]
                )
        self.assertEqual(code, 0)
        self.folder = output
        code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertTrue(
            reconcile_report([self.archive], [output], SELECTION, album_map=self.mapping)[
                "metadata_and_albums_validated"
            ]
        )

        config = self.root / "empty.json"
        config.write_text("{}")
        roundtrip = self.root / "roundtrip"
        result = subprocess.run(
            [
                "immich-go",
                "archive",
                "from-folder",
                "--config",
                str(config),
                "--write-to-folder",
                str(roundtrip),
                "--save-config=false",
                "--log-file",
                str(self.root / "roundtrip.log"),
                str(output),
            ],
            capture_output=True,
            text=True,
            env=os.environ | {"TZ": "UTC"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            reconcile_report([self.archive], [roundtrip], SELECTION, album_map=self.mapping)[
                "metadata_and_albums_validated"
            ]
        )

    @staticmethod
    @settings(max_examples=20, deadline=None, derandomize=True)
    @given(st.dates(min_value=date(1971, 1, 1), max_value=date(2030, 12, 31)))
    def test_dated_mapping_is_deterministic_and_uses_utc_album_date(day):
        from datetime import UTC, datetime, timedelta

        from immich_ctl.takeout import Coverage

        next_day = day + timedelta(days=1)
        coverage = Coverage(
            album_metadata={
                directory: [
                    {
                        "title": "Trip",
                        "date": {
                            "timestamp": str(
                                int(
                                    datetime.combine(
                                        value, datetime.min.time(), tzinfo=UTC
                                    ).timestamp()
                                )
                            )
                        },
                    }
                ]
                for directory, value in (("a", day), ("b", next_day))
            }
        )
        mapping = dated_names(coverage, {"a": "Trip", "b": "Trip"})
        assert mapping == {"a": f"Trip ({day.isoformat()})", "b": f"Trip ({next_day.isoformat()})"}
        assert mapping == dated_names(coverage, {"b": "Trip", "a": "Trip"})
