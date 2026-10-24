"""Upload an extracted immich-go archive using private account credentials."""

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from immich_ctl.config import ConfigError, load_config
from immich_ctl.upload_output import run_upload


class FolderUploadError(Exception):
    pass


def upload_folder(
    folder: Path, config: Path, *, dry_run: bool = False, log_file: Path | None = None
) -> int:
    """Delegate media and JSON sidecar handling to immich-go's folder reader."""
    folder = folder.expanduser().resolve()
    if not folder.is_dir() or not any(folder.iterdir()):
        raise FolderUploadError("Upload requires an existing, non-empty folder.")
    server, key = load_config(config.expanduser())
    binary = shutil.which("immich-go")
    if binary is None:
        raise FolderUploadError("immich-go was not found on PATH (tested with version 0.32.0).")
    if log_file is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        log_file = Path("logs") / f"folder-upload-{stamp}-{uuid4().hex[:8]}.log"
    log_file = log_file.expanduser().resolve()
    if log_file.is_relative_to(folder) or log_file == config.expanduser().resolve():
        raise FolderUploadError("The log must be outside the source folder and account config.")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("x", encoding="utf-8"):
        log_file.chmod(0o600)
    print(f"Log: {json.dumps(str(log_file))}", flush=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("IMMICH_GO_")}
    env["TZ"] = "UTC"
    with tempfile.TemporaryDirectory(prefix="immich-ctl-") as temporary:
        private_config = Path(temporary) / "immich-go.json"
        with private_config.open("x", encoding="utf-8") as stream:
            private_config.chmod(0o600)
            json.dump({"upload": {"server": server.removesuffix("/api"), "api-key": key}}, stream)
        command = [
            binary,
            "upload",
            "from-folder",
            "--config",
            str(private_config),
            "--recursive=true",
            "--ignore-sidecar-files=false",
            "--folder-as-album=NONE",
            "--folder-as-tags=false",
            "--date-from-name=false",
            "--manage-burst=NoStack",
            "--manage-heic-jpeg=NoStack",
            "--manage-raw-jpeg=NoStack",
            "--overwrite=false",
            "--pause-immich-jobs=false",
            "--save-config=false",
            "--api-trace=false",
            "--skip-verify-ssl=false",
            "--no-ui",
            "--time-zone=UTC",
            "--on-errors=stop",
            "--dry-run=" + str(dry_run).lower(),
            "--log-type=JSON",
            "--log-level=INFO",
            "--log-file",
            str(log_file),
            "--",
            str(folder),
        ]
        result = run_upload(command, env, dry_run=dry_run)
    if result.returncode:
        print(f"immich-go failed; inspect {json.dumps(str(log_file))}.", file=sys.stderr)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Extracted archive, including its .JSON sidecars")
    parser.add_argument("--config", required=True, type=Path, metavar="INI")
    parser.add_argument("--dry-run", action="store_true", help="Contact server without uploading")
    parser.add_argument("--log-file", type=Path, metavar="FILE", help="New log (default: logs/)")
    args = parser.parse_args(argv)
    try:
        return upload_folder(args.folder, args.config, dry_run=args.dry_run, log_file=args.log_file)
    except (FolderUploadError, ConfigError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
