"""Small synthetic exports exercising counting rather than real personal data."""

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

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
            self.assertEqual(set(titles), {"Photos from 2023", "Old album", "Empty"})
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
