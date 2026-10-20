"""Small synthetic exports exercising counting rather than real personal data."""

import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).with_name("immich-describe-takeout")
inspect_archives = runpy.run_path(str(SCRIPT))["inspect_archives"]


class TakeoutTests(unittest.TestCase):
    def test_split_export_dedup_dates_and_albums(self):
        prefix = "Takeout/Google Photos/"
        photo = {"title": "image", "url": "photo-1",
                 "photoTakenTime": {"timestamp": "1672531200"}}
        video = {"title": "clip.MOV", "url": "video-1",
                 "photoTakenTime": {"timestamp": "invalid"}}
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.zip"
            second = Path(temporary) / "second.zip"
            with zipfile.ZipFile(first, "w") as archive:
                for directory in ("Photos from 2023", "Trip", "Trip(1)"):
                    archive.writestr(prefix + directory + "/image.supplemental-metadata.json",
                                     json.dumps(photo))
                for directory in ("Trip", "Trip(1)", "Empty"):
                    archive.writestr(prefix + directory + "/metadata.json",
                                     json.dumps({"title": "Trip" if directory != "Empty" else "Empty"}))
                archive.writestr(prefix + "Trip/clip.mov.json", json.dumps(video))
                archive.writestr(prefix + "Trip/broken.json", "{")
                archive.writestr(prefix + "Photos from 2023/orphan.jpg", b"orphan")
            with zipfile.ZipFile(second, "w") as archive:
                for directory in ("Photos from 2023", "Trip", "Trip(1)"):
                    archive.writestr(prefix + directory + "/image.jpg", b"photo")
                # Duplicate entry across ZIP parts must not inflate counts.
                archive.writestr(prefix + "Trip/clip.mov.json", json.dumps(video))
            items, memberships, titles, raw, warnings, sidecars = inspect_archives([first, second])
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
            result = subprocess.run([str(SCRIPT), str(first), str(second)],
                                    capture_output=True, text=True, check=True)
            self.assertIn("Trip [folder: Trip(1)]", result.stdout)
            self.assertIn("Unknown", result.stdout)

    def test_missing_ids_remain_separate_and_invalid_zip_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for directory in ("Album A", "Album B"):
                    archive.writestr(f"Takeout/Google Photos/{directory}/photo.jpg.json",
                                     json.dumps({"title": "photo.jpg", "photoTakenTime": {}}))
            items, _, _, _, warnings, _ = inspect_archives([path])
            self.assertEqual(len(items), 2)
            self.assertEqual(sum(warnings.values()), 2)
            path.write_text("not a zip")
            result = subprocess.run([str(SCRIPT), str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Error reading Takeout", result.stderr)


if __name__ == "__main__":
    unittest.main()
