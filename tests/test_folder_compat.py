"""Content-based detection of immich-go's local-duplicate album hazard."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from immich_ctl.folder_compat import album_duplicate_groups


class AlbumDuplicateTests(unittest.TestCase):
    def test_equal_sizes_are_not_proof_of_duplicates_and_albums_belong_to_checksum_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg")]
            for path, contents in zip(paths, [b"one", b"one", b"two", b"two", b"six"], strict=True):
                path.write_bytes(contents)
            metadata = {path: {} for path in paths}
            metadata[paths[4]] = {"albums": [{"title": "Trip"}]}
            self.assertEqual(album_duplicate_groups(paths, metadata), [])
            metadata[paths[1]] = {"albums": [{"title": "Trip"}]}
            self.assertEqual(album_duplicate_groups(paths, metadata), [paths[:2]])
            metadata[paths[2]] = {"albums": [{"title": "Other"}]}
            self.assertEqual(album_duplicate_groups(paths[::-1], metadata), [paths[:2], paths[2:4]])

    def test_unique_sizes_and_albumless_groups_need_no_media_hashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in ("one.jpg", "two.jpg", "three.jpg")]
            for path, contents in zip(paths, [b"one", b"two", b"longer"], strict=True):
                path.write_bytes(contents)
            metadata = {path: {} for path in paths}
            metadata[paths[2]] = {"albums": [{"title": "Trip"}]}
            with patch("immich_ctl.folder_compat.hashlib.file_digest") as digest:
                self.assertEqual(album_duplicate_groups(paths, metadata), [])
            digest.assert_not_called()
