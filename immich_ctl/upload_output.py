"""Keep immich-go's simulated upload output distinct from completed operations."""

import re
import subprocess
import tempfile


def dry_run_line(line: str) -> str:
    """Relabel known report fields without rewriting filenames or error messages."""
    labels = {
        "Asset Tracking Report:": "Dry-run Asset Tracking Report:",
        "Event Report:": "Dry-run Event Report:",
        "Processed:": "Would process:",
        "Discarded:": "Would skip:",
        "uploaded successfully": "would upload",
        "metadata updated": "metadata processing (simulated)",
    }
    for old, new in labels.items():
        if re.match(r"^\s*" + re.escape(old) + r"(?=\s|$)", line):
            return line.replace(old, new, 1)
    if line.startswith("Immich read "):
        return re.sub(r"\bUploaded(?=\s+\d)", "Would upload", line)
    return line


def run_upload(
    command: list[str], env: dict[str, str], *, dry_run: bool
) -> subprocess.CompletedProcess:
    if not dry_run:
        return subprocess.run(command, env=env, check=False)
    print("DRY RUN: asset upload and metadata report entries below are simulated.", flush=True)
    print("Waiting for the preview; the raw immich-go log retains upstream wording.", flush=True)
    # Spool to disk rather than retaining potentially large tool output in memory.
    # Merge stderr so diagnostics survive alongside the relabelled report.
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as output:
        result = subprocess.run(
            command, env=env, check=False, stdout=output, stderr=subprocess.STDOUT
        )
        output.seek(0)
        for line in output:
            print(dry_run_line(line), end="")
    return result
