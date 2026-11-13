"""Disposable SHA-1/SHA-256 cache shared by local media checks, never server results."""

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any


def fingerprint(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Local checksum input must be a regular file, not a symlink: " + str(path))
    return state(info)


def state(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def hash_file(path: Path, before: list[int]) -> dict[str, str]:
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with path.open("rb") as stream:
        if state(os.fstat(stream.fileno())) != before:
            raise OSError("Local media changed while hashing; retry the check.")
        while chunk := stream.read(1024 * 1024):
            sha1.update(chunk)
            sha256.update(chunk)
        if state(os.fstat(stream.fileno())) != before or fingerprint(path) != before:
            raise OSError("Local media changed while hashing; retry the check.")
    return {"sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}


def default_path() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return root / "immich-ctl" / "local-hashes.sqlite3"


class LocalHashes:
    def __init__(self, folders: list[Path], *, enabled: bool = True):
        self.db: sqlite3.Connection | None = None
        if not enabled or os.environ.get("IMMICH_CTL_NO_HASH_CACHE") == "1":
            return
        path = default_path().expanduser().absolute()
        # A disposable cache must never modify inputs, follow links or replace other files.
        try:
            if path.resolve() != path or any(path.is_relative_to(p.resolve()) for p in folders):
                return
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.exists():
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return
            if info.st_size:
                with path.open("rb") as stream:
                    if stream.read(16) != b"SQLite format 3\x00":
                        return
            path.chmod(0o600)
            self.db = sqlite3.connect(path, timeout=1)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS local_hashes_v1 "
                "(path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                "sha1 TEXT NOT NULL, sha256 TEXT NOT NULL)"
            )
            self.db.commit()
        except (OSError, sqlite3.Error):
            self.close()

    def __enter__(self) -> "LocalHashes":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    def checksums(self, path: Path) -> dict[str, str]:
        path = path.expanduser().absolute()
        before = fingerprint(path)
        key = str(path.resolve())
        encoded = json.dumps(before)
        if self.db is not None:
            try:
                row = self.db.execute(
                    "SELECT fingerprint, sha1, sha256 FROM local_hashes_v1 WHERE path = ?", (key,)
                ).fetchone()
                if (
                    row is not None
                    and row[0] == encoded
                    and all(
                        isinstance(value, str)
                        and len(value) == length
                        and all(c in "0123456789abcdef" for c in value)
                        for value, length in zip(row[1:], (40, 64), strict=True)
                    )
                ):
                    if fingerprint(path) != before:
                        raise OSError("Local media changed while reading cached hashes; retry.")
                    return {"sha1": row[1], "sha256": row[2]}
            except (sqlite3.Error, UnicodeEncodeError):
                # Unix surrogate-escaped filenames are readable but not SQLite TEXT.
                self.close()
        result = hash_file(path, before)
        if self.db is not None:
            try:
                self.db.execute(
                    "INSERT OR REPLACE INTO local_hashes_v1 VALUES (?, ?, ?, ?)",
                    (key, encoded, result["sha1"], result["sha256"]),
                )
                self.db.commit()
            except (sqlite3.Error, UnicodeEncodeError):
                self.close()
        return result
