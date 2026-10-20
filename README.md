# Immich migration utilities

`immich-describe-takeout` reads Google Photos Takeout ZIP archives locally and
reports photos and videos by capture year and album. It does not extract files,
contact Immich, or modify the archives. Requires Python 3; no packages to install.

```bash
./immich-describe-takeout ~/Downloads/takeout-20261018T153319Z-1-001.zip
# Supply every part of a split export together, for one person at a time:
./immich-describe-takeout ~/Downloads/takeout-20261018T153319Z-1-*.zip
# Optionally save the report:
./immich-describe-takeout ~/Downloads/takeout-20261018T153319Z-1-*.zip > takeout-report.txt
```

Year and album counts describe Google Photos items using their JSON sidecars.
Repeated Google Photos URLs count once overall and once per album. Missing IDs
are counted separately and reported as warnings. Separate album folders with the
same title remain separate. Album metadata supplies the display name when present;
otherwise the folder name is used. `Photos from YYYY` folders are excluded from
albums unless they explicitly contain album metadata.
The special `Archive`, `Trash` and `Failed videos` folders are also excluded
unless they contain album metadata. Their items remain in the year counts.

Years come from `photoTakenTime`, interpreted in UTC. Dates near New Year can
therefore differ from the year displayed in a local time zone. Missing or invalid
capture dates appear under `Unknown`; file modification dates are not substituted.

The separate ZIP inventory counts files by extension, including repeated album
copies, edited versions and Live Photo companion clips. Files without usable JSON
appear only in this inventory. Metadata counts do not establish byte identity,
visual uniqueness, or whether Immich will import or skip a file. The script does
not check that each sidecar has a matching media file. Keep all export parts
together for a meaningful report.

Run the synthetic counting checks with `python3 -m unittest -v`.

## Test server credentials

`immich-ping` reads a per-account INI file and makes exactly one authenticated
`GET /api/users/me` request. It prints the account name, email and user ID so you
can confirm which library the key belongs to. It requires only `user.read` and
does not upload, edit or delete anything. This tests connectivity and account
access, not whether the key has all permissions needed for an import.

Create a private config outside this directory:

```bash
mkdir -p ~/.config/immich-ctl
chmod 700 ~/.config/immich-ctl
install -m 600 account.example.ini ~/.config/immich-ctl/james.ini
# Edit james.ini to supply your actual URL and API key, then:
./immich-ping --config ~/.config/immich-ctl/james.ini
```

Use a separate file for the second account and select it with `--config`.
Use your HTTPS PixelUnion URL, with or without a trailing `/api`. TLS certificate
checks stay enabled. Redirects are refused so credentials cannot be forwarded.
The default request timeout is 15 seconds; change it with `--timeout 30`.
The script never prints the key or server response bodies on errors.
Exit status is 0 on success, 1 on a failed check, and 2 for invalid arguments.
