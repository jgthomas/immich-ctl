"""Copy named Takeout members unchanged, without inferring metadata associations."""

import argparse
import hashlib
import json
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

from immich_ctl.takeout import google_photos_path


def safe_path(name: str) -> bool:
    """Require an ordinary relative ZIP path, without normalization surprises."""
    return (
        bool(name)
        and not PurePosixPath(name).is_absolute()
        and "\\" not in name
        and "\x00" not in name
        and all(part not in {"", ".", ".."} for part in name.split("/"))
    )


def extract(archive: Path, output: Path, members: list[str], *, dry_run: bool) -> list[dict]:
    """Validate all selections, then copy into an exclusively created directory."""
    if not members or any(not safe_path(name) for name in members):
        raise ValueError("Select relative file paths beneath Google Photos, without . or .. parts.")
    if len(set(members)) != len(members):
        raise ValueError("Each member must be selected only once.")
    if output.exists() or output.is_symlink():
        raise ValueError("Output must be a new directory; existing paths are never overwritten.")
    if not output.parent.is_dir():
        raise ValueError("Output parent directory must already exist.")
    with zipfile.ZipFile(archive) as source:
        found: dict[str, list[zipfile.ZipInfo]] = {name: [] for name in members}
        for entry in source.infolist():
            relative = google_photos_path(entry.filename)
            if relative is not None and str(relative) in found:
                found[str(relative)].append(entry)
        selected = []
        for name in members:
            entries = found[name]
            if len(entries) != 1:
                raise ValueError(f"Member missing or ambiguous: {json.dumps(name)}")
            entry = entries[0]
            mode = entry.external_attr >> 16
            if (
                not safe_path(entry.filename)
                or entry.is_dir()
                or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
            ):
                raise ValueError(f"Not a safe regular ZIP member: {json.dumps(name)}")
            selected.append((name, entry))
        # A selected file cannot also serve as another selection's parent directory.
        for name in members:
            if any(str(parent) in found for parent in PurePosixPath(name).parents):
                raise ValueError("Selected file and directory paths conflict.")
        report = [{"path": name, "size": entry.file_size} for name, entry in selected]
        if dry_run:
            return report
        output.mkdir()  # Exclusive creation also protects against a race after preflight.
        try:
            for row, (name, entry) in zip(report, selected, strict=True):
                destination = output.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                checksum = hashlib.sha256()
                with source.open(entry) as reader, destination.open("xb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        writer.write(chunk)
                        checksum.update(chunk)
                row["sha256"] = checksum.hexdigest()
        except BaseException:
            shutil.rmtree(output)
            raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="One Google Photos Takeout ZIP")
    parser.add_argument(
        "--member",
        action="append",
        required=True,
        help="Exact path beneath Google Photos; repeat to select more files",
    )
    parser.add_argument("--output", required=True, type=Path, help="New manual review directory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List selections without writing or reading media bytes",
    )
    args = parser.parse_args(argv)
    try:
        report = extract(
            args.archive.expanduser(), args.output.expanduser(), args.member, dry_run=args.dry_run
        )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as error:
        print("Error: " + json.dumps(str(error)), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    for row in report:
        print(json.dumps(row))
    print(f"Files {'selected' if args.dry_run else 'extracted'}: {len(report)}")
    print(
        "Dry run: no files written; media integrity not checked."
        if args.dry_run
        else "Bytes copied unchanged. Metadata associations are not inferred; review before upload."
    )
    return 0
