"""One lazy Takeout summary per command, never a cross-command metadata cache."""

from pathlib import Path
from typing import Any

from immich_ctl.collision_cache import archive_state
from immich_ctl.takeout import Inspection, inspect_archives


def canonical_paths(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(path.expanduser().resolve() for path in paths))


class ArchiveInspection:
    def __init__(self, paths: list[Path]):
        self.paths = canonical_paths(paths)
        self._result: Inspection | None = None
        self._state: list[dict[str, Any]] | None = None

    def get(self, paths: list[Path]) -> Inspection:
        if canonical_paths(paths) != self.paths:
            raise ValueError("Archive inspection does not match the requested ZIPs.")
        before = archive_state(self.paths)
        if self._result is None:
            result = inspect_archives(self.paths)
            if archive_state(self.paths) != before:
                raise ValueError("Source archives changed during inspection; rerun the command.")
            self._result, self._state = result, before
        elif before != self._state:
            raise ValueError("Source archives changed during the command; rerun it.")
        return self._result
