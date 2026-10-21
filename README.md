# immich-ctl

Standalone scripts for Immich and Google Photos Takeout archives.

## Requirements

- **Python 3.11+**. The Takeout script uses `datetime.UTC`, introduced in Python 3.11.
- No third-party runtime packages.
- For `immich-ping`: your Immich server URL and an API key with `user.read` permission.
- For extraction and upload: `immich-go` on PATH (tested with 0.32.0).
- For development: [uv](https://docs.astral.sh/uv/).

## Describe a Takeout export

Report photos and videos by capture year and album, plus metadata-to-file coverage.
Supply all ZIP parts from **one person's export** together:

```bash
./immich-describe-takeout ~/Downloads/takeout-EXPORT-*.zip

# List unresolved metadata and unpaired files:
./immich-describe-takeout --details ~/Downloads/takeout-EXPORT-*.zip

# Save the report:
./immich-describe-takeout ~/Downloads/takeout-EXPORT-*.zip > takeout-report.txt
```

The script reads filenames and JSON without extracting or modifying the archive.
When interpreting the report:

- Year and album totals count metadata items, deduplicated by Google Photos ID.
- Capture years use UTC; missing, zero or conflicting dates are `Unknown`.
- Confirmed albums have album metadata; possible album folders are listed separately.
- File coverage uses conservative filename matching. An unmatched item is not
  necessarily missing; an unpaired file might be an edit or Live Photo clip.
- File inventory counts are separate from metadata totals. The report does not
  check media contents, identify visual duplicates or predict immich-go imports.

## Select dates from a Takeout export

`immich-takeout` queries metadata, extracts a local archive, or uploads directly
from the ZIPs. Pass all ZIP parts from one export together. Dates are UTC and
inclusive: `2022`, `2022-07`, `2022-07-15`, `2020,2022`, or
`2022-07-15,2023-01-05`.

```bash
# Read-only counts and albums; add --list to list selected sidecars:
./immich-takeout query --date-range 2022 ~/Downloads/takeout-EXPORT-*.zip

# Preview extraction without writing files:
./immich-takeout extract --date-range 2022 --output /path/to/extracted-2022 \
  --dry-run ~/Downloads/takeout-EXPORT-*.zip

# Extract media and generated JSON metadata to a new or empty folder:
./immich-takeout extract --date-range 2022 --output /path/to/extracted-2022 \
  ~/Downloads/takeout-EXPORT-*.zip

# Simulate an upload using the account INI described below:
./immich-takeout upload --date-range 2020,2022 \
  --config ~/.config/immich-ctl/ACCOUNT.ini --dry-run ~/Downloads/takeout-EXPORT-*.zip
```

- Remove `--dry-run` to upload. Extraction is optional; uploads read ZIPs directly.
  Source ZIPs are never modified.
- Extraction creates year/month folders and `.JSON` metadata compatible with
  `immich-go upload from-folder`.
- Queries count metadata IDs across all states, excluding unknown/conflicting
  dates. Counts do not predict immich-go matching, filtering or deduplication.
- Extraction dry runs only report the plan; upload dry runs contact the server.
- Archived items are included; trashed/unmatched items are excluded. Add
  `--include-partner` to include partner-shared media.
- Uploads sync albums, disable overwrite and leave server jobs running.
- Logs go to `./logs/`; use `--log-file FILE` for a new custom path.
- Credentials use a private temporary config. Ambient immich-go config and
  `IMMICH_GO_*` settings are ignored.

## Upload an extracted folder

Keep the extracted media and `.JSON` sidecars together. Preview the upload:

```bash
./immich-upload-folder /path/to/extracted-2022 \
  --config ~/.config/immich-ctl/ACCOUNT.ini --dry-run
```

Remove `--dry-run` to upload. The command reads folders recursively and uses
sidecar album associations, without creating albums from year/month folders.
It disables overwrite, leaves server jobs running, and uses the same private
credential handling as Takeout uploads. Dry runs contact the server; both modes
write logs to `./logs/` or a new `--log-file FILE` outside the source folder.
Source files are not modified. Metadata fidelity depends on immich-go and Immich;
keep the original Takeout ZIPs.

## Test Immich credentials

Create a private config from [account.example.ini](account.example.ini):

```bash
mkdir -p ~/.config/immich-ctl
chmod 700 ~/.config/immich-ctl
install -m 600 account.example.ini ~/.config/immich-ctl/ACCOUNT.ini
```

Replace `ACCOUNT` with a name for the account. Edit the file with your HTTPS
PixelUnion URL and API key, then run:

```bash
./immich-ping --config ~/.config/immich-ctl/ACCOUNT.ini
```

Use a separate INI file per user. `--config` is required. The key needs `user.read`.
Ping makes one read-only request and prints the account name, email and user ID,
without printing the key. It does not verify upload permissions.

All commands and subcommands support `--help`.

## Development

From the repository root:

```bash
uv sync --locked --dev --group mutation# Set up dependencies
make check                             # Run all checks
uv run --locked ruff format .          # Apply formatting
```

Checks include:

- Unit tests and Hypothesis property tests on synthetic archives.
- Extraction integration test when `immich-go` is installed.
- Ruff lint and formatting checks.
- Mypy type checking for the scripts and shared modules.
- Installed-command `--help` smoke checks.
- Mutmut mutation testing of shared helpers in `immich_ctl/`.

Mutation tests run on copies of the helpers; the extensionless CLI scripts are
not mutated. Surviving mutations may reveal missing assertions or equivalent
behaviour and do not fail the checks. Local output shows a summary; full output
is saved to `mutants/check.log`. Inspect survivors with:

```bash
uv run --locked --no-sync mutmut results
uv run --locked --no-sync mutmut browse
```

GitHub Actions runs the standard checks on Python 3.11–3.14 using synthetic data
and no Immich credentials. Mutation testing runs separately as a non-blocking
report, with full logs and details in the `mutation-report` artifact for 14 days.
