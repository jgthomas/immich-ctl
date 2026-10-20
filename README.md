# Immich migration utilities

Standalone commands for interacting with PixelUnion Immich and inspecting local
Google Photos exports. Requires **Python 3.11 or newer**. The commands use only
the standard library and can be run directly without installing the project.

`immich-describe-takeout` reads Google Photos Takeout ZIP archives locally and
reports photos and videos by capture year and album. It does not extract files,
contact Immich, or modify the archives.

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

Run the synthetic counting checks with `python3 -m unittest discover -v`.

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

## Development

Executable scripts stay in the repository root. Tests and their shared script
loader live in `tests/`; each command remains independently runnable and has no
dependency on the tests. `pyproject.toml` declares the supported Python version
and configures formatting, import sorting and lint checks with
[Ruff](https://docs.astral.sh/ruff/configuration/), including the extensionless
executables. Key functions carry type annotations; a static type checker is not
currently configured.

We use [uv](https://docs.astral.sh/uv/guides/projects/) to manage the development
environment. `.python-version` selects Python 3.14 for development; the scripts
continue to support Python 3.11 and newer. `pyproject.toml` declares dependencies,
with Ruff in the `dev` dependency group. `uv.lock` records resolved dependencies
and should be committed alongside these files. `.venv/` stays local and ignored.

After installing uv, recreate the environment from the repository root:

```bash
uv sync --locked
```

uv creates `.venv/` and installs the project and development tools. It can download
the selected Python version if it is not available locally. No environment
activation is needed. On a new machine, clone the repository, run the command
above, and configure account credentials separately outside the repository.

Run tests and checks:

```bash
uv run --locked python -m unittest discover -v
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Apply formatting with `uv run --locked ruff format .`. To run a command through
the managed environment:

```bash
uv run --locked immich-ping --config ~/.config/immich-ctl/james.ini
uv run --locked immich-describe-takeout ~/Downloads/takeout-*.zip
```

Direct execution with `./immich-ping` and `./immich-describe-takeout` continues to
work without uv or third-party runtime packages. Tests use synthetic archives
and mocked API responses, without real credentials or access to a live server.

Manage dependencies through uv rather than installing ad hoc into `.venv/`:

```bash
uv add --dev PACKAGE          # Add a development tool
uv add PACKAGE                # Add a runtime dependency, if needed in future
uv lock --upgrade-package ruff # Update within the declared version constraint
```

Ruff is currently pinned exactly; changing its version requires updating that
constraint, for example with `uv add --dev 'ruff==NEW_VERSION'`. Commit changes to
both `pyproject.toml` and `uv.lock`. Use `uv lock` after manually editing dependency
declarations; normal setup and checks use `--locked` to prevent silent lockfile
changes. External tools such as `immich-go` and private account INI files are
managed separately from Python dependencies.
