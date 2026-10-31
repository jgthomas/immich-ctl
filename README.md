# immich-ctl

Standalone scripts for Immich and Google Photos Takeout archives.

## Requirements

- **Python 3.11+**. The Takeout script uses `datetime.UTC`, introduced in Python 3.11.
- No third-party runtime packages.
- For `immich-ping`: your Immich server URL and an API key with `user.read` permission.
- For validation: an API key with `user.read`, `asset.read` and `album.read` permissions.
- For server upload-risk audits: `user.read` and `asset.read`; also `album.read`
  when the input has album memberships.
- For extraction, recovery and upload: `immich-go` on PATH (tested with 0.32.0).
- For development: [uv](https://docs.astral.sh/uv/).

## Migration runbook

Start with a year containing few photos. Run these steps from the repository root
in Bash.

In the commands below, **`--output` creates a new folder**; **`--extracted` reads an
existing folder**. The folders stay separate. Later checks read them together to
account for all the files prepared for upload.

1. **Set the year, paths and account.** Replace the export pattern and account name;
   include all ZIP parts from the same export.

   ```bash
   year=2022
   archives=(~/Downloads/takeout-EXPORT-*.zip)
   normal_extract="$HOME/Pictures/immich-$year"
   recovered_edits="${normal_extract}-recovered"
   reviewed_files="${normal_extract}-reviewed"
   config="$HOME/.config/immich-ctl/ACCOUNT.ini"
   ```

2. **Inspect the selection and numbered files.** Add `--details` to investigate findings.

   ```bash
   ./immich-takeout query --date-range "$year" "${archives[@]}"
   ./immich-takeout audit "${archives[@]}"
   ```

3. **Extract from the ZIPs.** Creates `$normal_extract` containing the media and
   sidecars immich-go can match normally, including many `(n)` files. Keep each
   media file and its `.JSON` sidecar together.

   ```bash
   ./immich-takeout extract --date-range "$year" --output "$normal_extract" "${archives[@]}"
   ```

4. **Extract the numbered edits missed by step 3.** Reads the ZIPs and checks
   `$normal_extract` to avoid recovering files already present. Writes recoverable
   omissions into `$recovered_edits`, leaving `$normal_extract` unchanged.

   ```bash
   ./immich-takeout recover --date-range "$year" --extracted "$normal_extract" \
     --output "$recovered_edits" "${archives[@]}"
   ```

   Add `--dry-run` to preview recovery without creating files.

5. **Check what is still missing or incorrect.** Compares the original ZIPs with
   the combined contents of the two folders. Both `--extracted` arguments are
   **inputs**: this command prints a report and creates no media or folders.

   ```bash
   ./immich-takeout reconcile --date-range "$year" \
     --extracted "$normal_extract" --extracted "$recovered_edits" --details "${archives[@]}"
   ```

   Omit the recovery input if step 4 created no folder. Review missing files,
   metadata/album differences, conflicts and unexpected media. If manual recovery
   is needed, put verified files and sidecars in `$reviewed_files`, then repeat
   this check with `--extracted "$reviewed_files"` added. Record deferred exceptions
   and keep unresolved files out of upload folders. Unknown-date findings are export-wide.

6. **Repair stored filenames, then check upload risks.** Repair each prepared folder
   so its JSON `fileName` values match the media filenames. Omit recovery commands
   and inputs if no recovery folder was created. The audit makes no server changes;
   review its warnings about retained metadata, replacement and skips before proceeding.

   ```bash
   ./immich-repair-filenames "$normal_extract"
   ./immich-repair-filenames "$recovered_edits"
   ./immich-takeout audit --config "$config" --date-range "$year" \
     --extracted "$normal_extract" --extracted "$recovered_edits" "${archives[@]}"
   ```

7. **Upload and validate each prepared folder.** Repeat these commands with
   `$recovered_edits` and `$reviewed_files` for folders that exist.

   ```bash
   ./immich-upload-folder "$normal_extract" --config "$config"
   ./immich-validate-folder "$normal_extract" --config "$config" --details
   ```

   To preview an upload first, add `--dry-run` to `immich-upload-folder` and inspect
   its log. A dry run uploads nothing.

   Investigate skipped uploads and validation findings. Check dates, GPS, albums and
   playback. Keep the ZIPs and Google Photos until independent backup and restore
   testing are complete.

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
- Extraction and direct ZIP uploads reject matching basenames/sizes with different
  contents before starting, checking the whole export across all dates. Identical
  album copies are allowed. Check the reported paths and process unsafe groups
  separately with their metadata.
- Queries count metadata IDs across all states, excluding unknown/conflicting
  dates. A separate file table counts directly matched media, candidate edited
  variants and their total. Edits without their own metadata inherit dates only
  from an unambiguous original in the same folder, with matching extension and
  duplicate index. These counts do not predict immich-go's extraction or upload.
- Extraction dry runs only report the plan; upload dry runs contact the server.
- Archived items are included; trashed/unmatched items are excluded. Add
  `--include-partner` to include partner-shared media.
- Uploads sync albums, disable overwrite and leave server jobs running.
- Logs go to `./logs/`; use `--log-file FILE` for a new custom path.
  Recorded errors cause a failing exit status even if immich-go returns success;
  partial output is retained for investigation.
- Upload dry-run console reports say `Would upload` and label metadata processing
  as simulated. The report appears after the preview finishes; raw logs retain
  immich-go's original wording.
- Credentials use a private temporary config. Ambient immich-go config and
  `IMMICH_GO_*` settings are ignored.

## Audit Takeout files and upload risks

```bash
./immich-takeout audit ~/Downloads/takeout-EXPORT-*.zip
```

Pass all ZIP parts from one export. The read-only report counts numbered media
and `-edited` variants by capture year, separating direct sidecar matches,
candidate original sidecars, ambiguous associations and missing metadata.
Add `--details` for filenames and proposed sidecars, or `--json` for the full report.
Files with unresolved, missing or conflicting dates appear under `Unknown`.
Duplicate album titles across source directories are listed separately for the
whole export. Choose distinct titles for separate albums, or explicitly accept a merge.

Potentially affected edits are filename-based candidates for the numbered-edit
matching issue, not confirmed extraction failures. Literal parentheses are
preserved; numbering does not prove duplication. Other edit suffixes are not
recognised. Album-folder copies count separately; repeated ZIP entries count once.
The audit reads filenames and JSON only, without extraction or server access.

Add `--config` for a read-only comparison with your Immich library:

```bash
./immich-takeout audit --config /path/to/account.ini --date-range 2022 \
  --extracted /path/to/extracted-2022 ~/Downloads/takeout-EXPORT-*.zip
```

- Warns when existing checksums have metadata that immich-go will leave unchanged.
- Warns about different-checksum filename/time matches: larger incoming files can
  replace and permanently delete existing assets; equal/smaller files can be skipped
  and their albums attached to the existing asset.
- Checks possible collisions within the input and reports uncertain comparisons.
- Warns about ambiguous source titles used by the input and multiple destination
  albums sharing a relevant title. One matching destination album is normal reuse.
- Flags identical input bytes with differing album memberships, listing the files
  and combined memberships to preserve. Consolidate memberships on every copy or
  prepare one upload per checksum; the script does not change albums or remove files.
- Repeat `--extracted` for recovery/reviewed folders. Omit it to inspect archive
  candidates, whose filenames can differ from the prepared uploads.

Server comparisons hash media and may take time. `--date-range` filters archive
candidates; supplied upload folders are always audited in full, with warnings for
files outside the requested dates. Numbered-file counts remain export-wide. Findings are snapshot warnings,
not an upload guarantee; successful reports return exit status zero even with
warnings. No files or server data are changed. Use `--details` or `--json` for findings.
Server scope includes trash, matching immich-go's inventory; locked and
external-library assets are excluded. Trashed checksum matches are flagged because
they may prevent an active upload. Native sidecar omissions are reported as unverified
when comparing existing metadata; the audit does not infer zero/false source values.

## Recover omitted numbered edits

Compare numbered edits in the ZIPs against an existing extraction by SHA-256:

```bash
./immich-takeout recover --date-range 2022 --extracted /path/to/extracted-2022 \
  --output /path/to/recovered-2022 --dry-run ~/Downloads/takeout-EXPORT-*.zip
```

- Remove `--dry-run` to create a **new**, separate upload-ready folder and a sibling
  `recovered-2022.manifest.json` recording source paths, associations and checksums.
- Recovers missing numbered `-edited` variants only when an exact original has
  unambiguous metadata and a capture date in the selected range. Conflicting
  metadata stays unresolved. Trashed and partner items are excluded; use
  `--include-partner` to include partner items.
- Preserves source album metadata, including narrative/location enrichments.
  Uses immich-go to generate `.JSON` sidecars, then verifies recovered bytes and
  supported metadata before publishing. Conflicting albums remain unresolved.
  Recovery filenames include a checksum prefix to avoid collisions with originals.
- Matching bytes confirm local file coverage only, not metadata or album coverage.
  Other missing files and unclear associations still require review.
- Dry runs write nothing and need no immich-go. Recovery contacts no server and
  leaves source ZIPs and the existing extraction unchanged. Upload and validate
  the recovery folder using the commands below.

## Reconcile Takeout files with extracted folders

```bash
./immich-takeout reconcile --date-range 2022 \
  --extracted /path/to/extracted-2022 --extracted /path/to/recovered-2022 \
  ~/Downloads/takeout-EXPORT-*.zip
```

- Repeat `--extracted` for normal and recovery folders; omit folders not yet created.
  The report compares SHA-256 checksums, including renamed and byte-identical copies.
- Lists missing selected media, conflicting source metadata and local JSON problems.
  Rejected own sidecars block inherited edit dates and remain named exceptions.
  Unknown dates, ambiguous associations and unpaired files appear separately as
  **export-wide exceptions**, without assigning dates from folder names.
- Checks identical content across date ranges, including other years, before
  certifying selected files. Possible album folders without metadata stay unresolved.
  Every local media file is accounted for; unexpected files are classified as
  outside-date, excluded, unresolved source content or unmatched to the export.
- Compares source capture dates, descriptions, GPS, favourite/archive flags and
  exact album associations with generated sidecars, including album descriptions
  and locations. Photo GPS and album locations are checked separately; album
  locations are not assumed to be photo GPS. Handles omitted zero values,
  EXIF-to-Google photo GPS fallbacks and identical album copies.
  Unclassified source files are listed separately; other metadata fields are unverified.
- Album-folder copies count as separate source files; distinct checksum counts are
  also shown. Metadata records without matching media filenames are listed separately.
- Add `--details` to list all files and export-wide exceptions, or `--json` for the full report.
  Trashed/partner items are excluded when their metadata is consistent; add
  `--include-partner` to include partner items.
- Reads source media and local files without writing files, invoking immich-go or
  contacting Immich. Checksums read selected media, export-wide exceptions and
  relevant files from other dates,
  so large exports can take time. Exit `0` means the report was generated, even with findings;
  operational errors return `1`. These are local extraction checks; use the validator
  below to check Immich after upload.

## Repair extracted filenames

Align each native JSON sidecar's `fileName` with its actual media filename:

```bash
./immich-repair-filenames /path/to/extracted-2022
```

Add `--dry-run` to preview. Works recursively on normal or recovered extractions,
changing mismatched `fileName` values in place. Media, sidecar filenames and other
metadata values stay unchanged; rewritten JSON is formatted with two-space indentation.
Already matching sidecars are left untouched, so reruns make no further changes.

All media/sidecar pairs must pass inspection before writing starts. Missing,
ambiguous, incompatible or malformed sidecars and unsupported files stop the run.
Each JSON replacement is atomic and preserves its permissions. No server or
immich-go access is needed; keep the Takeout ZIPs for re-extraction if needed.

This removes stored-name aliases, not duplicate images. Matching basenames in other
folders or on the server can still collide: rerun reconciliation and the upload-risk
audit after repair. It does not repair metadata already uploaded to Immich.

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
Dry-run console reports use the same simulated-action labels as Takeout uploads.
Both modes reject missing/incompatible native sidecars, unreadable or unsupported
files and paths containing glob characters (`*`, `?`, brackets or backslashes).
Parenthesised `(n)` suffixes are allowed. Keep the `software` version marker in
manually prepared native sidecars and reconcile their fields before uploading.
Prepare verified copies with safe names when necessary; source files are not renamed.
Recorded errors or malformed logs produce a failing exit status even if immich-go
returns success.
Source files are not modified. Metadata fidelity depends on immich-go and Immich;
keep the original Takeout ZIPs.

## Validate an extracted folder against Immich

After uploading, check the extracted files against the same user's library:

```bash
./immich-validate-folder /path/to/extracted-2022 \
  --config ~/.config/immich-ctl/ACCOUNT.ini
```

- Matches media by SHA-1 checksum, including archived and hidden assets; partner
  copies and trashed/locked assets do not confirm an upload.
- Compares explicit JSON capture dates, descriptions, GPS (within 0.00001 degrees),
  favourite/archive flags and album memberships. Missing supported fields are
  reported as unverified, including omitted zero coordinates and false flags;
  their source values cannot be established from the native sidecar alone.
- Reports missing files, ambiguous matches, differences and incomplete checks.
  Unsupported metadata (including tags), XMP, unreadable sidecars and skipped files
  cannot establish complete validation. Album names are checked; duplicate names
  cannot identify a particular album, and extra memberships are allowed.
  Album descriptions, locations and other nested album fields are explicitly
  unchecked and produce an `incomplete` result.
- Add `--details` to list passing files too, or `--json` for the full report.
  Exit codes: `0` all available checks passed, `2` findings/incomplete checks,
  `1` operational error; interrupted runs return `130`.
- Reads local files and contacts Immich without changing either. Metadata jobs
  may need to finish before rerunning. This does not test playback, visual
  duplicates, original downloads or backup restoration. Keep the Takeout ZIPs.

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
uv sync --locked --dev --group mutation # Set up dependencies
make check                             # Run all checks
uv run --locked ruff format .           # Apply formatting
```

Checks include:

- Unit tests and Hypothesis property tests on synthetic archives.
- Extraction/recovery integration tests when `immich-go` is installed; mandatory
  against pinned 0.32.0 in a separate CI job.
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
and no Immich credentials. Mutation testing runs locally through `make check`
only.
CI integrations use the real immich-go binary and synthetic archives; live Immich
uploads and server metadata jobs still require a trial against a server.
