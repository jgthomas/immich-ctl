"""Load private account INI files without exposing their contents in errors."""

import configparser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ConfigError(Exception):
    pass


def load_config(path: Path) -> tuple[str, str]:
    """Read account credentials and return the API base URL and API key."""
    config = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as stream:
            config.read_file(stream)
        server = config.get("immich", "server").strip()
        key = config.get("immich", "api_key").strip()
    except OSError:
        raise ConfigError("Cannot read the config file; check its path and permissions.") from None
    except (configparser.Error, UnicodeError):
        # Parser errors can quote lines containing the secret.
        raise ConfigError(
            "Invalid config: expected [immich] with server and api_key entries."
        ) from None
    if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ConfigError("api_key must be a non-empty value without spaces or control characters.")
    try:
        url = urlsplit(server)
        _ = url.port  # Validate malformed ports too.
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(c.isspace() for c in server)
        ):
            raise ValueError()
    except ValueError:
        raise ConfigError(
            "server must be an HTTP(S) URL without credentials, query or fragment."
        ) from None
    root = url.path.rstrip("/")
    if not root.endswith("/api"):
        root += "/api"
    endpoint = urlunsplit((url.scheme, url.netloc, root, "", ""))
    return endpoint, key
