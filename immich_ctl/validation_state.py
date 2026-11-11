"""Private local hash checkpoints; server metadata is always fetched afresh."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def fingerprint(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class Checkpoint:
    def __init__(self, path: Path | None, folder: Path):
        self.db: sqlite3.Connection | None = None
        if path is None:
            return
        path = path.expanduser().absolute()
        if path.resolve() != path or path.is_relative_to(folder.resolve()):
            raise OSError("Checkpoint must be outside the extraction, without symlink components.")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        elif not path.is_file() or path.stat().st_nlink != 1:
            raise OSError("Checkpoint must be a regular file with no additional hard links.")
        elif path.stat().st_size:
            with path.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\x00":
                    raise OSError(
                        "Existing checkpoint is not a SQLite database; choose a new path."
                    )
        path.chmod(0o600)
        try:
            self.db = sqlite3.connect(path)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS hashes "
                "(path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, sha1 TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS progress (id INTEGER PRIMARY KEY, report TEXT NOT NULL)"
            )
            self.db.commit()
        except sqlite3.Error:
            self.close()
            raise OSError(
                "Cannot open validation checkpoint; choose a new checkpoint path."
            ) from None

    def close(self) -> None:
        if self.db is not None:
            self.db.close()

    def checksum(self, path: Path) -> str:
        before = fingerprint(path)
        saved = None
        if self.db is not None:
            saved = self.db.execute(
                "SELECT fingerprint, sha1 FROM hashes WHERE path = ?", (str(path),)
            ).fetchone()
        if saved is not None and saved[0] == json.dumps(before):
            value = saved[1]
            if len(value) == 40 and all(c in "0123456789abcdef" for c in value):
                return value
        with path.open("rb") as stream:
            value = hashlib.file_digest(stream, "sha1").hexdigest()
        if fingerprint(path) != before:
            raise OSError("Local media changed while hashing; retry validation.")
        if self.db is not None:
            self.db.execute(
                "INSERT OR REPLACE INTO hashes VALUES (?, ?, ?)",
                (str(path), json.dumps(before), value),
            )
            self.db.commit()
        return value

    def record(self, report: dict[str, Any]) -> None:
        if self.db is not None:
            self.db.execute("INSERT OR REPLACE INTO progress VALUES (1, ?)", (json.dumps(report),))
            self.db.commit()
