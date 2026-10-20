"""Account validation boundaries highlighted by mutation testing."""

import tempfile
import unittest
from pathlib import Path

from immich_ctl.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "account.ini"

    def write_config(self, server: str, key: str = "synthetic-secret") -> None:
        self.path.write_text(f"[immich]\nserver={server}\napi_key={key}\n", encoding="utf-8")

    def test_unreadable_file_error_explains_what_to_check(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            load_config(self.path)  # The file has not been created.
        message = str(raised.exception).casefold()
        for term in ("file", "path", "permissions"):
            self.assertIn(term, message)

    def test_malformed_config_error_explains_expected_entries(self) -> None:
        secret = "synthetic-secret-without-a-section"
        self.path.write_text(secret, encoding="utf-8")
        with self.assertRaises(ConfigError) as raised:
            load_config(self.path)
        message = str(raised.exception).casefold()
        for term in ("config", "immich", "server", "api_key"):
            self.assertIn(term, message)
        self.assertNotIn(secret, message)

    def test_invalid_key_error_explains_key_constraints(self) -> None:
        secret = "synthetic secret with spaces"
        self.write_config("https://photos.example", secret)
        with self.assertRaises(ConfigError) as raised:
            load_config(self.path)
        message = str(raised.exception).casefold()
        for term in ("api_key", "non-empty", "spaces", "control"):
            self.assertIn(term, message)
        self.assertNotIn(secret, message)

    def test_invalid_server_error_explains_url_constraints(self) -> None:
        self.write_config("ftp://photos.example")
        with self.assertRaises(ConfigError) as raised:
            load_config(self.path)
        message = str(raised.exception).casefold()
        for term in ("server", "http", "url", "credentials", "query", "fragment"):
            self.assertIn(term, message)

    def test_api_key_character_boundaries(self) -> None:
        self.write_config("https://photos.example", "!valid-%-key~")
        self.assertEqual(load_config(self.path)[1], "!valid-%-key~")
        for key in ("", "a b", "a\x00b", "a\x1fb", "a\x7fb", "aéb"):
            with self.subTest(key=repr(key)):
                self.write_config("https://photos.example", key)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_server_rejects_each_invalid_component(self) -> None:
        for server in (
            "ftp://photos.example",
            "https:///",
            "https://name@photos.example",
            "https://photos.example:invalid",
            "https://photos.example:70000",
            "https://photos.example?secret=value",
            "https://photos.example#secret",
            "https://photos.ex ample",
            "https://photos.ex\tample",
        ):
            with self.subTest(server=server):
                self.write_config(server)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_server_normalisation_preserves_supported_urls_and_paths(self) -> None:
        for server, expected in (
            ("http://photos.example:2283", "http://photos.example:2283/api"),
            ("https://photos.example/", "https://photos.example/api"),
            ("https://photos.example/api/", "https://photos.example/api"),
            ("https://photos.example/galleryX/", "https://photos.example/galleryX/api"),
        ):
            with self.subTest(server=server):
                self.write_config(server)
                self.assertEqual(load_config(self.path)[0], expected)


if __name__ == "__main__":
    unittest.main()
