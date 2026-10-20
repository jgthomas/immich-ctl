# immich-ctl

Standalone scripts for PixelUnion Immich and Google Photos Takeout archives.
Requires **Python 3.11+**, with no third-party runtime packages.

## Describe a Takeout export

Report photos and videos by capture year and album, plus metadata-to-file coverage.
Supply all ZIP parts from **one person's export** together:

```bash
./immich-describe-takeout ~/Downloads/takeout-20261018T153319Z-1-*.zip

# List unresolved metadata and unpaired files:
./immich-describe-takeout --details ~/Downloads/takeout-20261018T153319Z-1-*.zip

# Save the report:
./immich-describe-takeout ~/Downloads/takeout-20261018T153319Z-1-*.zip > takeout-report.txt
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

Both commands support `--help`.

## Development

Use [uv](https://docs.astral.sh/uv/) from the repository root. Development uses
Python 3.14, selected by `.python-version`.

```bash
uv sync --locked
uv run --locked python -m unittest discover -v
uv run --locked ruff check .
uv run --locked ruff format --check .

# Apply formatting:
uv run --locked ruff format .
```

Scripts stay in the repository root; tests and their script loader live in
`tests/`. Tests use synthetic archives and mocked API responses. Commands can
also run through uv, for example `uv run --locked immich-ping --help`.

Add dependencies with `uv add PACKAGE` or `uv add --dev PACKAGE`. After manually
editing dependencies, run `uv lock`. Keep `pyproject.toml`, `uv.lock` and
`.python-version` in Git; keep `.venv/` and real credentials out. Install external
tools such as `immich-go` separately.
