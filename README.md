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

Remove `--dry-run` from the upload command to import. Extraction is optional;
uploads read the original ZIPs directly. Neither operation modifies the ZIPs.
The extracted archive uses immich-go's year/month folders and `.JSON` metadata,
which its `upload from-folder` command can read; it is not an unchanged Takeout copy.

Queries count metadata IDs, skip unknown/conflicting capture timestamps, and
include all metadata states. They do not predict immich-go's file matching,
deduplication or filtering results. Extraction dry runs show this metadata report
and the plan: the installed immich-go's archive dry run writes files, so it is
not invoked. Upload dry runs invoke immich-go and contact the server.

Extraction and upload include archived items, exclude trashed/unmatched items,
and exclude partner-shared media unless `--include-partner` is supplied. Uploads
sync albums, disable overwrite and leave server jobs running. Logs are retained
in `./logs/`; `--log-file FILE` chooses a new log path. API keys are passed through
a private temporary config, never command-line arguments. These commands ignore
ambient immich-go config and `IMMICH_GO_*` environment settings.

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

Use [uv](https://docs.astral.sh/uv/) from the repository root.

```bash
uv sync --locked
uv run --locked python -m unittest discover -v
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy

# Apply formatting:
uv run --locked ruff format .
```

Scripts stay in the repository root; shared config and Takeout helpers live in
`immich_ctl/`, and tests in `tests/`. Tests use synthetic archives and mocked API
responses; an optional extraction integration test runs when immich-go is installed. Commands can
also run through uv, for example `uv run --locked immich-ping --help`.

Mypy checks the three executable scripts and shared modules, including unannotated
function bodies. Strict mode is not enabled.

GitHub Actions runs the tests, Ruff, mypy and installed-command checks on pushes
and pull requests. CI uses synthetic data and requires no Immich credentials.
