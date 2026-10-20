"""Small synthetic exports exercising counting rather than real personal data."""

import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

from immich_ctl import takeout as metadata
from tests.script_loader import PROJECT_ROOT, load_script

SCRIPT = PROJECT_ROOT / "immich-describe-takeout"
takeout = load_script("immich-describe-takeout")
inspect_archives = takeout.inspect_archives


class TakeoutTests(unittest.TestCase):
    def write_export(self, path: Path, entries: dict[str, object]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(
                    f"Takeout/Google Photos/{name}",
                    data if isinstance(data, bytes) else json.dumps(data),
                )

    def test_report_counts_and_table_values(self) -> None:
        items = {
            ("url", "photo-1"): ("2022", "Photos"),
            ("url", "photo-2"): ("2023", "Photos"),
            ("url", "video"): ("2023", "Videos"),
            ("url", "other"): ("Unknown", "Other"),
        }
        self.assertEqual(metadata.counts(items, items), Counter(Photos=2, Videos=1, Other=1))
        self.assertEqual(
            metadata.counts([("url", "video"), ("url", "other")], items),
            Counter(Videos=1, Other=1),
        )
        self.assertEqual(metadata.counts([], items), Counter())
        output = io.StringIO()
        with redirect_stdout(output):
            metadata.print_table("Album", [("Trip", Counter(Photos=3, Videos=2, Other=1))])
        header, row = output.getvalue().splitlines()
        self.assertEqual(header.casefold().split(), ["album", "photos", "videos", "other", "total"])
        self.assertEqual(row.split(), ["Trip", "3", "2", "1", "6"])

    def test_table_escapes_control_characters_in_album_names(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            metadata.print_table("Album", [("Trip\n\t\r\x1b[31m\\end", Counter(Photos=1))])
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1].split()[0], r"Trip\n\t\r\x1b[31m\end")
        for control in ("\t", "\r", "\x1b"):
            self.assertNotIn(control, output.getvalue())

    def test_missing_numbered_files_never_match_the_unindexed_original(self) -> None:
        for sidecar in (
            "photo(1).jpg.json",
            "photo.jpg(1).json",
            "photo.jpg.supplemental-metadata(1).json",
        ):
            with self.subTest(sidecar=sidecar), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "export.zip"
                self.write_export(
                    path,
                    {
                        "Album/photo.jpg": b"original",
                        f"Album/{sidecar}": {
                            "title": "photo.jpg",
                            "url": "numbered",
                            "photoTakenTime": {"timestamp": "1672531200"},
                        },
                    },
                )
                *_, coverage = inspect_archives([path])
                self.assertEqual(coverage.missing_items, {("url", "numbered")})
                self.assertFalse(coverage.matched_items)
                self.assertFalse(coverage.ambiguous_items)
                self.assertEqual({key[0] for key in coverage.unmatched_files}, {"Album/photo.jpg"})

    def test_skipped_entries_do_not_hide_later_media(self) -> None:
        photo = {
            "title": "photo.jpg",
            "url": "later",
            "photoTakenTime": {"timestamp": "1672531200"},
        }
        prefix = "Takeout/Google Photos/"
        skipped_entries = {
            "directory": (prefix + "Ignored/", b""),
            "outside Google Photos": ("Takeout/Other/data.json", b"{}"),
            "duplicate": (
                prefix + "Album/seed.jpg.json",
                json.dumps({**photo, "title": "seed.jpg", "url": "seed"}),
            ),
            "memory titles": (prefix + "Album/user-generated-memory-titles.json", b"{}"),
            "oversized JSON": (prefix + "Album/large.json", b" " * (8 * 1024 * 1024 + 1)),
            "invalid JSON": (prefix + "Album/broken.json", b"{"),
            "non-object JSON": (prefix + "Album/list.json", b"[]"),
            "unrecognised JSON": (prefix + "Album/unrecognised.json", b"{}"),
        }
        for case, (name, contents) in skipped_entries.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "export.zip"
                paths = []
                if case == "duplicate":
                    seed = Path(temporary) / "seed.zip"
                    with zipfile.ZipFile(seed, "w") as archive:
                        archive.writestr(name, contents)
                    paths.append(seed)
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(name, contents)
                    archive.writestr(prefix + "Album/photo.jpg.json", json.dumps(photo))
                    archive.writestr(prefix + "Album/photo.jpg", b"photo")
                paths.append(path)
                items, _, _, raw, _, sidecars, coverage = inspect_archives(paths)
                self.assertEqual(items[("url", "later")], ("2023", "Photos"))
                self.assertEqual(coverage.matched_items, {("url", "later")})
                self.assertEqual(raw, Counter(Photos=1))
                self.assertEqual(sidecars, 2 if case == "duplicate" else 1)
                self.assertEqual(coverage.directories, {"Album"})

    def test_partial_media_metadata_is_recognised_without_inventing_capture_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.zip"
            self.write_export(
                path,
                {
                    "Album/created.jpg.json": {
                        "title": "created.jpg",
                        "creationTime": {"timestamp": "1672531200"},
                    },
                    "Album/url.jpg.json": {"title": "url.jpg", "url": "url-only"},
                    "Album/metadata.json": {"title": "metadata.jpg", "url": "named-metadata"},
                },
            )
            items, memberships, titles, _, warnings, sidecars, coverage = inspect_archives([path])
            self.assertEqual(sidecars, 3)
            self.assertEqual(Counter(items.values()), Counter({("Unknown", "Photos"): 3}))
            self.assertEqual(items[("url", "url-only")], ("Unknown", "Photos"))
            self.assertEqual(items[("url", "named-metadata")], ("Unknown", "Photos"))
            self.assertEqual(len(memberships["Album"]), 3)
            self.assertFalse(titles)
            self.assertEqual(sum(warnings.values()), 1)  # The creation-only item lacks an ID.
            self.assertTrue(all(not times for times in coverage.capture_times.values()))

    def test_album_titles_fall_back_to_folder_for_missing_or_invalid_titles(self) -> None:
        for invalid in (None, 42, ["title"], {"title": "nested"}):
            with self.subTest(title=invalid), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "export.zip"
                self.write_export(
                    path,
                    {
                        "Current/metadata.json": {"title": invalid},
                        "Old/album.json": {"albumData": {"title": invalid}},
                        "Missing/metadata.json": {},
                    },
                )
                items, memberships, titles, _, warnings, sidecars, _ = inspect_archives([path])
                self.assertEqual(titles, {"Current": "Current", "Old": "Old", "Missing": "Missing"})
                self.assertEqual(memberships, {folder: set() for folder in titles})
                self.assertFalse(items)
                self.assertFalse(warnings)
                self.assertEqual(sidecars, 0)

    def test_conflict_and_missing_sidecar_counts_accumulate(self) -> None:
        entries = {}
        for identity in ("first", "second"):
            for directory, extension, timestamp in (
                ("A", "jpg", "1672531200"),
                ("B", "mov", "1704067200"),
            ):
                entries[f"{directory}/{identity}.{extension}.json"] = {
                    "title": f"{identity}.{extension}",
                    "url": identity,
                    "photoTakenTime": {"timestamp": timestamp},
                }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.zip"
            self.write_export(path, entries)
            items, _, _, _, warnings, sidecars, coverage = inspect_archives([path])
            self.assertEqual(
                items, {("url", name): ("Unknown", "Other") for name in ("first", "second")}
            )
            for category in ("conflicting capture years", "conflicting media types"):
                self.assertEqual(
                    sum(
                        count
                        for message, count in warnings.items()
                        if category in message.casefold()
                    ),
                    2,
                )
            self.assertEqual(sum(warnings.values()), 4)
            self.assertEqual(sidecars, 4)
            self.assertEqual(coverage.sidecars_without_local_match, 4)
            self.assertEqual(coverage.missing_items, set(items))

    def test_json_size_boundary_and_warning_counts(self) -> None:
        limit = 8 * 1024 * 1024
        payload = json.dumps({"title": "photo.jpg", "url": "boundary"}).encode()
        entries = {
            "Album/boundary.jpg.json": payload.ljust(limit, b" "),
            "Album/oversized-1.json": payload.ljust(limit + 1, b" "),
            "Album/oversized-2.json": payload.ljust(limit + 2, b" "),
            "Album/broken-1.json": b"{",
            "Album/broken-2.json": b"\xff",
            "Album/non-object-1.json": [],
            "Album/non-object-2.json": None,
            "Album/unrecognised-1.json": {},
            "Album/unrecognised-2.json": {"title": "no media fields"},
            "Album/user-generated-memory-titles.json": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.zip"
            self.write_export(path, entries)
            items, _, _, _, warnings, sidecars, _ = inspect_archives([path])
            self.assertEqual(items, {("url", "boundary"): ("Unknown", "Photos")})
            self.assertEqual(sidecars, 1)
            for category in (
                "larger than 8 mib",
                "unreadable json",
                "non-object json",
                "unrecognised json",
            ):
                self.assertEqual(
                    sum(
                        count
                        for message, count in warnings.items()
                        if category in message.casefold()
                    ),
                    2,
                )
            self.assertEqual(sum(warnings.values()), 8)

    def test_unknown_dates_and_zero_timestamp(self) -> None:
        for timestamp in ("0", 0, None, True, 12.5, "1.5", "not-a-date"):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(
                    takeout.capture_year({"photoTakenTime": {"timestamp": timestamp}}), "Unknown"
                )
        self.assertEqual(takeout.capture_year({"photoTakenTime": None}), "Unknown")
        self.assertEqual(
            takeout.capture_year({"creationTime": {"timestamp": "1672531200"}}), "Unknown"
        )
        self.assertEqual(takeout.capture_year({"photoTakenTime": {"timestamp": "-1"}}), "1969")

    def test_numbered_sidecars_and_ambiguous_extensionless_titles(self) -> None:
        def photo(title: str, identity: str) -> dict[str, object]:
            return {"title": title, "url": identity, "photoTakenTime": {"timestamp": "1672531200"}}

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.zip"
            self.write_export(
                path,
                {
                    "Photos from 2023/image.jpg": b"original",
                    "Photos from 2023/image(1).jpg": b"numbered",
                    "Photos from 2023/image.jpg.supplemental-metadata.json": photo(
                        "image.jpg", "base"
                    ),
                    "Photos from 2023/image.jpg.supplemental-metadata(1).json": photo(
                        "image.jpg", "one"
                    ),
                    "Photos from 2023/image.jpg.supplemental-metadata(2).json": photo(
                        "image.jpg", "missing"
                    ),
                    "Photos from 2023/old(1).jpg": b"old numbered export",
                    "Photos from 2023/old.jpg(1).json": photo("old.jpg", "old"),
                    "Photos from 2023/day.jpg": b"jpeg",
                    "Photos from 2023/day.gif": b"gif",
                    "Photos from 2023/day.supplemental-metadata.json": photo("day", "ambiguous"),
                    "Photos from 2023/short.supplemental-metadata.json": photo(
                        "full-name.jpg", "long"
                    ),
                    "Photos from 2023/full-name.jpg": b"long filename",
                    "Photos from 2023/image-edited.jpg": b"edit",
                    "Photos from 2023/image.mp4": b"possible live clip",
                },
            )
            items, _, _, _, _, _, coverage = inspect_archives([path])
            self.assertEqual(len(items), 6)
            self.assertEqual(
                coverage.matched_items, {("url", name) for name in ("base", "one", "old", "long")}
            )
            self.assertEqual(coverage.missing_items, {("url", "missing")})
            self.assertEqual(coverage.ambiguous_items, {("url", "ambiguous")})
            self.assertEqual(len(coverage.matched_files), 4)
            self.assertEqual(len(coverage.unmatched_files), 4)
            result = subprocess.run(
                [str(SCRIPT), "--details", str(path)], capture_output=True, text=True, check=True
            )
            self.assertIn("Missing media match:", result.stdout)
            self.assertIn("Ambiguous media match:", result.stdout)
            self.assertIn("image-edited.jpg", result.stdout)

    def test_different_ids_claiming_one_file_are_not_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.zip"
            entries: dict[str, object] = {"Album/image.jpg": b"photo"}
            for suffix, identity in ((".json", "first"), (".supplemental-metadata.json", "second")):
                entries[f"Album/image.jpg{suffix}"] = {
                    "title": "image.jpg",
                    "url": identity,
                    "photoTakenTime": {"timestamp": "1672531200"},
                }
            self.write_export(path, entries)
            _, _, _, _, _, _, coverage = inspect_archives([path])
            self.assertFalse(coverage.matched_items)
            self.assertEqual(len(coverage.ambiguous_items), 2)
            self.assertEqual(len(coverage.unmatched_files), 1)

    def test_ambiguous_candidates_compete_with_exact_matches_for_different_ids(self) -> None:
        for same_identity in (False, True):
            for reverse in (False, True):
                with self.subTest(same_identity=same_identity, reverse=reverse):
                    with tempfile.TemporaryDirectory() as temporary:
                        path = Path(temporary) / "export.zip"
                        entries: dict[str, object] = {
                            "Album/image.jpg": b"jpeg",
                            "Album/image.png": b"png",
                            "Album/image.supplemental-metadata.json": {
                                "title": "image",
                                "url": "ambiguous",
                                "photoTakenTime": {"timestamp": "1672531200"},
                            },
                            "Album/image.jpg.supplemental-metadata.json": {
                                "title": "image.jpg",
                                "url": "ambiguous" if same_identity else "exact",
                                "photoTakenTime": {"timestamp": "1672531200"},
                            },
                        }
                        if reverse:
                            entries = dict(reversed(list(entries.items())))
                        self.write_export(path, entries)
                        _, _, _, _, _, _, coverage = inspect_archives([path])
                        self.assertFalse(coverage.missing_items)
                        if same_identity:
                            self.assertEqual(coverage.matched_items, {("url", "ambiguous")})
                            self.assertFalse(coverage.ambiguous_items)
                            self.assertEqual(
                                {key[0] for key in coverage.matched_files}, {"Album/image.jpg"}
                            )
                            self.assertEqual(
                                {key[0] for key in coverage.unmatched_files}, {"Album/image.png"}
                            )
                        else:
                            self.assertFalse(coverage.matched_items)
                            self.assertFalse(coverage.matched_files)
                            self.assertEqual(
                                coverage.ambiguous_items, {("url", "ambiguous"), ("url", "exact")}
                            )
                            self.assertEqual(coverage.ambiguous_sidecars, 2)
                            self.assertEqual(
                                {key[0] for key in coverage.unmatched_files},
                                {"Album/image.jpg", "Album/image.png"},
                            )

    def test_metadata_reconciliation_is_independent_of_archive_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.zip"
            second = Path(temporary) / "second.zip"
            self.write_export(
                first,
                {
                    "Photos from 2023/unknown.json": {
                        "title": "no extension",
                        "url": "same",
                        "photoTakenTime": None,
                    },
                    "Photos from 2023/conflict.jpg.json": {
                        "title": "conflict.jpg",
                        "url": "conflict",
                        "photoTakenTime": {"timestamp": "1672531200"},
                    },
                },
            )
            self.write_export(
                second,
                {
                    "Album/photo.jpg.json": {
                        "title": "photo.jpg",
                        "url": "same",
                        "photoTakenTime": {"timestamp": "1672531200"},
                    },
                    "Album/photo.jpg": b"photo",
                    "Album/conflict.mov.json": {
                        "title": "conflict.mov",
                        "url": "conflict",
                        "photoTakenTime": {"timestamp": "1704067200"},
                    },
                },
            )
            forward = inspect_archives([first, second])
            reverse = inspect_archives([second, first])
            self.assertEqual(forward, reverse)
            self.assertEqual(forward[0][("url", "same")], ("2023", "Photos"))
            self.assertEqual(forward[0][("url", "conflict")], ("Unknown", "Other"))
            self.assertEqual(forward[6].matched_items, {("url", "same")})
            self.assertEqual(sum(forward[4].values()), 2)

    def test_confirmed_inferred_and_special_album_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.zip"
            photo = {
                "title": "photo.jpg",
                "url": "one",
                "photoTakenTime": {"timestamp": "1672531200"},
            }
            self.write_export(
                path,
                {
                    "Photos from 2023/metadata.json": {
                        "title": "A real album named like a year folder"
                    },
                    "Photos from 2023/photo.jpg.json": photo,
                    "Old album/album.json": {"albumData": {"title": "Old-style album"}},
                    "Empty/metadata.json": {"title": "Empty"},
                    "Unconfirmed/photo.jpg": b"no metadata",
                    "Archive/photo.jpg.json": photo,
                    "Trash/photo.jpg.json": photo,
                    "Failed videos/photo.jpg.json": photo,
                },
            )
            _, _, titles, _, _, _, _ = inspect_archives([path])
            self.assertEqual(
                titles,
                {
                    "Photos from 2023": "A real album named like a year folder",
                    "Old album": "Old-style album",
                    "Empty": "Empty",
                },
            )
            result = subprocess.run(
                [str(SCRIPT), str(path)], capture_output=True, text=True, check=True
            )
            self.assertIn("3 confirmed by album metadata", result.stdout)
            self.assertIn("Possible album folders", result.stdout)
            self.assertIn("Unconfirmed", result.stdout)
            self.assertNotIn("Failed videos ", result.stdout)

    def test_split_export_dedup_dates_and_albums(self) -> None:
        prefix = "Takeout/Google Photos/"
        photo = {"title": "image", "url": "photo-1", "photoTakenTime": {"timestamp": "1672531200"}}
        video = {"title": "clip.MOV", "url": "video-1", "photoTakenTime": {"timestamp": "invalid"}}
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.zip"
            second = Path(temporary) / "second.zip"
            with zipfile.ZipFile(first, "w") as archive:
                for directory in ("Photos from 2023", "Trip", "Trip(1)"):
                    archive.writestr(
                        prefix + directory + "/image.supplemental-metadata.json", json.dumps(photo)
                    )
                for directory in ("Trip", "Trip(1)", "Empty"):
                    archive.writestr(
                        prefix + directory + "/metadata.json",
                        json.dumps({"title": "Trip" if directory != "Empty" else "Empty"}),
                    )
                archive.writestr(prefix + "Trip/clip.mov.json", json.dumps(video))
                archive.writestr(prefix + "Trip/broken.json", "{")
                archive.writestr(prefix + "Photos from 2023/orphan.jpg", b"orphan")
            with zipfile.ZipFile(second, "w") as archive:
                for directory in ("Photos from 2023", "Trip", "Trip(1)"):
                    archive.writestr(prefix + directory + "/image.jpg", b"photo")
                # Duplicate entry across ZIP parts must not inflate counts.
                archive.writestr(prefix + "Trip/clip.mov.json", json.dumps(video))
            items, memberships, titles, raw, warnings, sidecars, coverage = inspect_archives(
                [first, second]
            )
            self.assertEqual(len(items), 2)
            self.assertEqual(items[("url", "photo-1")], ("2023", "Photos"))
            self.assertEqual(items[("url", "video-1")], ("Unknown", "Videos"))
            self.assertEqual(len(memberships["Trip"]), 2)
            self.assertEqual(len(memberships["Trip(1)"]), 1)
            self.assertEqual(len(memberships["Empty"]), 0)
            self.assertEqual(titles["Trip(1)"], "Trip")
            self.assertEqual(raw["Photos"], 4)
            self.assertEqual(sidecars, 4)
            self.assertEqual(warnings["Unreadable JSON files (skipped)"], 1)
            self.assertEqual(coverage.matched_items, {("url", "photo-1")})
            self.assertEqual(coverage.missing_items, {("url", "video-1")})
            self.assertEqual(
                {key[0] for key in coverage.unmatched_files}, {"Photos from 2023/orphan.jpg"}
            )
            result = subprocess.run(
                [str(SCRIPT), str(first), str(second)], capture_output=True, text=True, check=True
            )
            self.assertIn("Trip [folder: Trip(1)]", result.stdout)
            self.assertIn("Unknown", result.stdout)

    def test_missing_ids_remain_separate_and_invalid_zip_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for directory in ("Album A", "Album B"):
                    archive.writestr(
                        f"Takeout/Google Photos/{directory}/photo.jpg.json",
                        json.dumps({"title": "photo.jpg", "photoTakenTime": {}}),
                    )
            items, _, _, _, warnings, _, _ = inspect_archives([path])
            self.assertEqual(len(items), 2)
            self.assertEqual(sum(warnings.values()), 2)
            path.write_text("not a zip")
            result = subprocess.run([str(SCRIPT), str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Error reading Takeout", result.stderr)


if __name__ == "__main__":
    unittest.main()
