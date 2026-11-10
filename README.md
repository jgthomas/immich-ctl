# immich-ctl

Standalone scripts for Immich and Google Photos Takeout archives.

## Requirements

- **Python 3.11+**. The Takeout script uses `datetime.UTC`, introduced in Python 3.11.
- No third-party runtime packages.
- For `immich-ping`: your Immich server URL and an API key with `user.read` permission.
- For validation: an API key with `user.read`, `asset.read` and `album.read` permissions.
- For server upload-risk audits: `user.read` and `asset.read`; also `album.read`
  when the input has album memberships.
- For creating albums or adding existing assets: see [album permissions](#create-an-album-and-add-existing-photos).
- For extraction, recovery and upload: `immich-go` on PATH (tested with 0.32.0).
- For `recover-companions`: `ffprobe` on PATH (from FFmpeg); immich-go is not invoked.
- For development: [uv](https://docs.astral.sh/uv/).

## Migration runbook

Run from the repository root in Bash, using all ZIP parts from the same export.
`--output` creates a new folder; `--extracted` reads an existing folder.
Add `--dry-run` to extraction, recovery or repair commands to preview local changes.
Work on one account at a time; all server commands must use that account's INI.

1. **Set the year, paths and account.** Keep the album mapping outside upload folders.

   ```bash
   trial_year=2022
   archives=(~/Downloads/takeout-EXPORT-*.zip)
   normal_extract="$HOME/Pictures/immich-$trial_year"
   recovered_edits="${normal_extract}-recovered"
   recovered_numbered="${normal_extract}-recovered-numbered"
   recovered_videos="${normal_extract}-recovered-videos"
   manual_extract="${normal_extract}-manual"
   album_map="$HOME/Pictures/immich-$trial_year-album-names.json"
   config="$HOME/.config/immich-ctl/ACCOUNT.ini"
   ```

2. **Query and audit the export.** Check candidate counts, albums and numbered-file patterns.

   ```bash
   ./immich-takeout query --date-range "$trial_year" "${archives[@]}"
   ./immich-takeout audit "${archives[@]}"
   ```

3. **Extract the year.** Keep the generated media and JSON sidecars together.

   ```bash
   ./immich-takeout extract --date-range "$trial_year" \
     --output "$normal_extract" "${archives[@]}"
   ```

4. **Recover omitted edits and ordinary numbered files separately.** Preview with
   `--dry-run` if needed. A `recover: 0` result needs no extraction; unresolved files
   with unknown dates can belong to other years.

   ```bash
   ./immich-takeout recover --date-range "$trial_year" --extracted "$normal_extract" \
     --output "$recovered_edits" "${archives[@]}"
   recovery_inputs=(--extracted "$normal_extract")
   if [[ -d "$recovered_edits" ]]; then recovery_inputs+=(--extracted "$recovered_edits"); fi
   ./immich-takeout recover-numbered --date-range "$trial_year" \
     "${recovery_inputs[@]}" \
     --output "$recovered_numbered" "${archives[@]}"
   ```

5. **Reconcile all existing folders against Takeout.** Build reusable folder arguments
   and save the report; inspect missing files and metadata/album findings. Include
   any manually recovered files in `$manual_extract`; rebuild these arguments if
   you add a folder later.

   ```bash
   folders=(); inputs=()
   for folder in "$normal_extract" "$recovered_edits" "$recovered_numbered" "$recovered_videos" "$manual_extract"; do
     [[ -d "$folder" ]] || continue
     folders+=("$folder")
     inputs+=(--extracted "$folder")
   done
   ./immich-takeout reconcile --date-range "$trial_year" "${inputs[@]}" \
     "${archives[@]}" > "/tmp/immich-$trial_year-reconcile.txt" 2>&1
   ```

6. **Resolve findings and repair file types and filenames.** Use [fixes and recovery](#fixes-and-recovery)
   for album names and manual recovery; record accepted uncertainties. These repairs
   change the local extraction, preserving media bytes.

   After these repairs, use [companion recovery](#recover-omitted-companion-videos)
   for omitted motion clips. Rebuild the step 5 folder arguments after adding a folder.

   ```bash
   for folder in "${folders[@]}"; do
     ./immich-repair-filetypes "$folder" --manifest "${folder}-filetypes.json" || break
     ./immich-repair-filenames "$folder" || break
   done
   ```

7. **Repeat reconciliation and audit the prepared upload folders.** Reuse the album
   mapping if one was created. Resolve or explicitly accept findings in both reports
   before proceeding; the uploader does not enforce audit results.

   ```bash
   album_args=()
   if [[ -f "$album_map" ]]; then album_args=(--album-map "$album_map"); fi
   ./immich-takeout reconcile --date-range "$trial_year" "${inputs[@]}" \
     "${album_args[@]}" "${archives[@]}" > "/tmp/immich-$trial_year-reconcile.txt" 2>&1
   ./immich-takeout audit --date-range "$trial_year" --config "$config" \
     "${inputs[@]}" "${archives[@]}" > "/tmp/immich-$trial_year-upload-audit.txt" 2>&1
   ```

8. **Preview each upload.** Check expected uploads, duplicates, albums and stacking;
   these previews contact Immich but upload no media.

   ```bash
   for folder in "${folders[@]}"; do
     ./immich-upload-folder "$folder" --config "$config" --dry-run || break
   done
   ```

9. **Upload each prepared folder.** If a command fails, inspect its log before
   continuing with the remaining folders; don't blindly retry an upload.

   ```bash
   for folder in "${folders[@]}"; do
     ./immich-upload-folder "$folder" --config "$config" || break
   done
   ```

10. **Validate and visually review.** Allow server processing to finish and inspect
    the saved results. Check dates, GPS, albums, image orientation, stacks, GIFs and
    video playback; a checksum match alone does not verify their presentation.

    ```bash
    for folder in "${folders[@]}"; do
      ./immich-validate-folder "$folder" --config "$config" \
        > "/tmp/${folder##*/}-validation.txt" 2>&1
    done
    ```

Keep the ZIPs and Google Photos until independent backup and restore testing are complete.

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
  Source ZIPs are never modified. Use the extracted-folder runbook when local
  filename, file-type or album-name repairs are needed; direct ZIP uploads bypass them.
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
- Extraction and direct ZIP uploads cache successful whole-export collision checks
  in `$XDG_CACHE_HOME/immich-ctl/collisions` (normally `~/.cache/immich-ctl/collisions`).
  This includes extraction dry runs; no extraction output is written. Changed ZIP
  identity, size, timestamps, input sets or checker code require a fresh check.
  The cache assumes downloaded ZIPs remain unchanged; it does not rehash entire
  archives on each use. Metadata is always read afresh and immich-go still performs
  its own processing. An unavailable or invalid cache falls back to checking.
  Use `--no-cache` to bypass cache reads/writes, `--refresh-cache` to rebuild a pass,
  or `--timings` to print phase durations to stderr. Deleting the cache is safe.
- Archived items are included; trashed/unmatched items are excluded. Add
  `--include-partner` to include partner-shared media.
- Uploads sync albums, set `--overwrite=false` and leave server jobs running.
  This flag does **not** disable immich-go's filename/time replacement heuristic;
  review the upload-risk audit before uploading.
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
- Flags identical input bytes with differing album memberships. For prepared
  folders, also flags identical files carrying any albums, even when memberships
  agree: immich-go 0.32.0 can queue an empty asset ID for a skipped local duplicate
  and fail an entire album request. `immich-upload-folder` blocks this case before
  previews or uploads. Prepare one copy per checksum with all required memberships;
  keep the other copies outside upload folders. No files are removed automatically.
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

## Create an album and add existing photos

Use exact asset UUIDs, as positional arguments or a whitespace-separated text file:

```bash
./immich-album --name "Repair review" --config "$config" \
  --ids-file /path/to/asset-ids.txt --dry-run
./immich-album --name "Repair review" --config "$config" \
  ASSET_UUID_1 ASSET_UUID_2
./immich-album --album-id ALBUM_UUID --config "$config" \
  --ids-file /path/to/more-ids.txt
```

A name creates an album or reuses a single owned album with that exact name.
Duplicate names require `--album-id`. No IDs creates an empty album. Repeat
`--ids-file` to combine files; repeated IDs are deduplicated. IDs must be UUID v4.
All selected assets must be owned by the configured account and outside trash;
preflight checks every ID before any writes. A dry run performs only reads.

Requires `user.read`, `asset.read`, `album.read`, `album.create` (for new albums)
and `albumAsset.create` (for additions). The server also checks access to share
individual assets. Existing memberships are accepted on repeat runs. Photos remain
in their original timeline/albums; the command never moves or deletes assets.

Additions run in batches of 100. A partial failure stops further batches and returns
nonzero, listing failed and unattempted IDs. Requests are not automatically retried.
The album ID is printed before additions; after an interruption or uncertain network
outcome, inspect the album and rerun with `--album-id`. An already-created album is
left intact if adding assets fails. Concurrent album creation can still produce
identical titles; avoid running two creators for the same name simultaneously.

## Fixes and recovery

Apply only the fixes needed by your reports. Keep the original ZIPs unchanged;
reconcile repaired files and repeat the upload-risk audit before uploading.
Generated `.JSON` sidecars are read by immich-go; Immich web/mobile uploads do
not interpret them. Keep manifests and mappings outside upload folders.

### Recover omitted numbered edits

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
- If nothing needs recovery, no output folder is created. Unresolved items with
  unknown dates remain visible across yearly runs; their folder names do not
  establish capture dates.
- Dry runs write nothing and need no immich-go. Recovery contacts no server and
  leaves source ZIPs and the existing extraction unchanged. Upload and validate
  the recovery folder using the commands below.

### Recover omitted numbered files

Run ordinary numbered-file recovery separately from numbered-edit recovery:

```bash
./immich-takeout recover-numbered --date-range 2022 \
  --extracted /path/to/extracted-2022 --extracted /path/to/recovered-edits-2022 \
  --output /path/to/recovered-numbered-2022 --dry-run ~/Downloads/takeout-EXPORT-*.zip
```

Remove `--dry-run` to recover into a new folder with a companion manifest.
This handles numbered non-edited media such as `photo(1).jpg` or `date(2).gif`,
including files whose Takeout JSON title has no extension. Only files with
unambiguous own metadata and a capture date in the selected range are recovered.
Trashed and partner items are excluded by default; `--include-partner` includes
partner items. Missing, ambiguous or conflicting metadata stays unresolved.

Repeat `--extracted` to check byte coverage across all existing folders. Media
bytes and available metadata are preserved using the same verified extraction
and publication safeguards as edit recovery. Dry runs write nothing and do not
invoke immich-go. Recovery contacts no server. Omit `--extracted` arguments for
folders that do not exist. Include newly recovered folders in subsequent
reconciliation, file-type/filename repair, upload-risk audits and validation;
reapply any album-name mapping repairs before uploading.

### Recover omitted companion videos

After repairing file types and filenames, use prepared extraction folders as the
metadata reference for omitted motion videos:

```bash
./immich-takeout recover-companions --date-range "$trial_year" \
  --extracted "$normal_extract" --output "${normal_extract}-recovered-videos" \
  --dry-run "${archives[@]}"
```

- Remove `--dry-run` to recover into a **new** folder, with a companion manifest
  outside it. Repeat `--extracted` to include other recovery folders in byte coverage.
- By default, examines unmatched video and extensionless filenames sharing a stem
  with a supplied still. Shared embedded Live Photo identifiers establish matches;
  filenames alone do not. Conflicting dates, GPS or still metadata remain unresolved.
- Preserves supported prepared metadata and combines albums across matching copies.
  Supply vetted folders with the intended partner/trash filtering already applied.
- Add `--include-unpaired` to also scan unmatched names and recover standalone videos
  with valid embedded capture dates. These use their own metadata and inherit no albums.
- Recovers QuickTime/MP4 containers; other formats remain unresolved.
- Checks dates in UTC, hashes source bytes, skips existing content, and publishes one
  copy per checksum with combined memberships. Prefixes names to avoid collisions.
  Source ZIPs and extracted files are unchanged; no server requests are made.
- Use `--json` for per-file provenance and unresolved findings. Temporary local files
  are used for ffprobe even during dry runs; no recovery output is written.
  Reconcile and audit the resulting folder before uploading. Live Photo presentation
  and pairing still need server validation.

### Extract individual files for manual review

Copy explicitly named files into a new directory, preserving their paths beneath
Google Photos. Repeat `--member` to select more files, including source JSON if
wanted. Paths are exact and case-sensitive; no wildcard expansion or automatic
sidecar selection is performed.

```bash
./immich-extract-manual /path/to/takeout.zip \
  --member 'Photos from 2022/example.MP4' \
  --output "$manual_extract" --dry-run
```

Remove `--dry-run` to extract. Existing output directories are refused. Extraction
checks ZIP integrity and reports SHA-256 checksums; a failed copy removes the new
output directory. Dry runs only check selections. Source archives are unchanged.
This does not generate upload metadata or infer capture dates, Live Photo links
or albums. For split exports, select the ZIP containing the member. Review the
files and establish metadata from their own embedded data, source JSON or reviewed
evidence; a nearby filename or a still-image companion alone is not proof.

Folder uploads require an immich-go native `.JSON` sidecar beside each media file.
Raw Takeout JSON is a different format. For manual files, prepare only supported,
verified fields and include the required `software` version marker; do not invent
dates or GPS to obtain a clean report. Include the manual folder in reconciliation,
repairs, upload-risk audits, uploads and validation. Uncertain source associations
may remain findings even after a reviewed manual recovery.

### Repair misleading image extensions

Some Takeout files contain JPEG data despite `.HEIC` or `.PNG` names. In Immich,
JPEGs named `.HEIC` can lose orientation metadata and appear stretched in the web
viewer. Correct known JPEG/PNG extensions before the first upload:

```bash
./immich-repair-filetypes "$normal_extract" \
  --manifest "${normal_extract}-filetypes.json" --dry-run
```

Remove `--dry-run` to apply. The script checks file signatures, renames each media
file and its native JSON sidecar, and changes only JSON `fileName`. Image bytes,
embedded orientation and other JSON fields remain unchanged. JPEG extensions
`.jpg`, `.jpeg` and `.jpe` are accepted; PNG uses `.png`. Other content is left
alone: this is a signature check, not full image decoding or validation.

Missing/invalid sidecars, unsafe paths and destination collisions (including case
variants) stop planning before any writes. The manifest must be outside the upload
folder. It records old/new paths and checksums before renaming and is marked
`complete` afterward. A failure rolls back the current file pair; earlier pairs
may already be complete. A manifest left `planned` needs inspection; rerun with a
new manifest path to finish remaining repairs. Completed pairs are idempotent.
Keep manifests outside upload folders and retain them for review.

Run filename-alias repair afterward, then repeat reconciliation and upload-risk
audits. Folder upload preflight, server validation and audits with `--extracted`
flag remaining JPEG/PNG signature mismatches; Takeout reconciliation does not
check file signatures. This command makes no server requests and does not fix
already uploaded assets; uploading the
same bytes again normally hits checksum deduplication.

### Repair extracted filenames

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

### Separate duplicate album names

Append each source album's UTC date to duplicate titles in extracted JSON:

```bash
./immich-repair-album-names --title "Trip" \
  --extracted /path/to/extracted --extracted /path/to/recovered \
  --mapping /path/to/album-names.json --dry-run ~/Downloads/takeout-EXPORT-*.zip
```

Remove `--dry-run` to apply. Repeat `--title`, or omit it to handle all duplicate
titles. Suffixes use the source album metadata's `date`, not photo capture dates.
Missing dates, colliding dated names or uncertain memberships stop all writes.
Membership is established by matching media bytes to source album directories.
Only album titles change; media and other sidecar values are preserved. Existing
matching repairs are left untouched. Each sidecar update is atomic; an interrupted
batch can be rerun using the same mapping.

Keep the mapping outside upload folders and reuse it when reconciling:

```bash
./immich-takeout reconcile --date-range 2022 \
  --extracted /path/to/extracted --extracted /path/to/recovered \
  --album-map /path/to/album-names.json ~/Downloads/takeout-EXPORT-*.zip
```

The mapping is checked against the original source directories and album dates.
This script makes no server requests and does not rename existing Immich albums.
Reapply it after re-extraction or additional recovery, then audit prepared folders.

### Review ambiguous metadata and album associations

A file can match the source bytes while its metadata association remains uncertain.
For example, one extensionless Takeout JSON title can match a JPG and a GIF;
immich-go creates separate sidecars using the same timestamp. Accept that timestamp
only after review. Filename repair gives the files distinct upload names but does
not establish which source metadata belongs to each file.

Album checks use exact-byte source copies. Edited versions can inherit their
original's albums even when Takeout does not repeat the edit in every album folder.
A reported extra membership is therefore not proof it is wrong: check Google
Photos or explicitly accept the difference before removing memberships.

Photo GPS and album locations are separate. An album location does not establish
where every photo was taken, and album-only coordinates need no photo-GPS repair.
Keep genuinely unresolved files out of upload folders unless their uncertainty is
explicitly accepted; record the paths and decision for later validation.

### Review upload behaviour and logs

- Byte-identical copies may be skipped; compare distinct checksums rather than
  expecting one server asset per local file.
- Existing checksum matches retain their current capture date, description, GPS
  and favourite/archive state; rerunning an upload is not a metadata repair.
  Validate those assets separately, especially after an interrupted upload.
- Different bytes with the same upload filename and a capture time within roughly
  five seconds can trigger replacement or skipping, including against trash.
  Filename repair removes aliases but does not eliminate every collision; review
  the prepared-folder audit before uploading. Larger files do not prove higher quality.
- immich-go 0.32.0 can stack same-stem, same-time images of different formats despite
  the burst/RAW/HEIC `NoStack` settings. Assets remain separate; use **Unstack** in
  Immich's web interface if the grouping is unwanted.
- The wrappers disable job pausing, but 0.32.0 still attempts to resume server jobs.
  This can log a thumbnail-job **403** after successful uploads. The wrapper returns
  a failure for logged errors: inspect the actual entry and validate uploaded files
  before retrying. Asset, metadata, album and stack failures still require attention.
- Native sidecars can omit empty descriptions, zero coordinates and false flags.
  The server validator reports those values as unverified rather than assuming
  they were preserved; compare with original Takeout metadata when needed.

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
Immich URL and API key, then run:

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
