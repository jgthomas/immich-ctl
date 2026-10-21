"""Properties of small synthetic, split Takeout exports."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.takeout import inspect_archives

# Bound filesystem work and keep examples consistent across mutation runs.
ARCHIVE_SETTINGS = settings(max_examples=25, deadline=None, derandomize=True)
ARCHIVE_PARTS = st.lists(
    st.fixed_dictionaries(
        {
            "folder": st.sampled_from(["Album", "Photos from 2023", "Trip 🌅"]),
            "stem": st.sampled_from(["image", "image(1)", "long name"]),
            "extension": st.sampled_from(["", ".jpg", ".mov"]),
            "media": st.sets(st.sampled_from([".jpg", ".png", ".mov", ".bin"]), max_size=3),
            "contents": st.binary(min_size=1, max_size=4),
            "identity": st.one_of(st.none(), st.integers(min_value=0, max_value=2)),
            "timestamp": st.sampled_from([None, "0", "invalid", "-1", "1672531200", "1704067200"]),
        }
    ),
    min_size=1,
    max_size=4,
)


def write_parts(directory: Path, records: list[dict]) -> list[Path]:
    """Allow repeated IDs, conflicting file contents, and missing/ambiguous matches."""
    paths = []
    for index, record in enumerate(records):
        path = directory / f"part-{index}.zip"
        prefix = f"Takeout/Google Photos/{record['folder']}/"
        title = record["stem"] + record["extension"]
        sidecar = {"title": title, "photoTakenTime": {"timestamp": record["timestamp"]}}
        if record["identity"] is not None:
            sidecar["url"] = f"photo-{record['identity']}"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(prefix + "metadata.json", json.dumps({"title": record["folder"]}))
            archive.writestr(prefix + title + ".supplemental-metadata.json", json.dumps(sidecar))
            for extension in sorted(record["media"]):
                archive.writestr(prefix + record["stem"] + extension, record["contents"])
        paths.append(path)
    return paths


class TakeoutPropertyTests(unittest.TestCase):
    # Stateless methods can be rerun by mutmut without changing Hypothesis executors.
    @staticmethod
    @ARCHIVE_SETTINGS
    @given(ARCHIVE_PARTS, st.data())
    def test_zip_part_order_does_not_change_inspection(records, data):
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_parts(Path(temporary), records)
            order = data.draw(st.permutations(paths), label="ZIP part order")
            assert inspect_archives(paths) == inspect_archives(list(order))

    @staticmethod
    @ARCHIVE_SETTINGS
    @given(ARCHIVE_PARTS)
    def test_repeated_zip_parts_do_not_change_inspection(records):
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_parts(Path(temporary), records)
            assert inspect_archives(paths) == inspect_archives(paths + paths)

    @staticmethod
    @ARCHIVE_SETTINGS
    @given(ARCHIVE_PARTS)
    def test_coverage_partitions_items_and_files(records):
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_parts(Path(temporary), records)
            items, memberships, _, raw, _, _, coverage = inspect_archives(paths)
            matched = coverage.matched_items
            ambiguous = coverage.ambiguous_items
            missing = coverage.missing_items
            assert not matched & ambiguous
            assert not matched & missing
            assert not ambiguous & missing
            assert matched | ambiguous | missing == set(items)
            assert not coverage.matched_files & coverage.unmatched_files
            assert len(coverage.matched_files | coverage.unmatched_files) == sum(raw.values())
            assert set(coverage.capture_times) == set(items)
            for members in memberships.values():
                assert members <= set(items)
