import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError


loader = importlib.machinery.SourceFileLoader("immich_ping", str(Path(__file__).with_name("immich-ping")))
spec = importlib.util.spec_from_loader(loader.name, loader)
ping = importlib.util.module_from_spec(spec)
loader.exec_module(ping)


class PingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "account.ini"
        self.key = "test-secret-with-%-character"
        self.path.write_text(f"[immich]\nserver = https://photos.example/api/\napi_key = {self.key}\n")

    def invoke(self, opener):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(ping, "build_opener", return_value=opener), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = ping.main(["--config", str(self.path)])
        self.assertNotIn(self.key, stdout.getvalue() + stderr.getvalue())
        return status, stdout.getvalue(), stderr.getvalue()

    def test_one_authenticated_get_and_account_output(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"id":"user-123","name":"Test User","email":"test@example.com"}'
        status, output, error = self.invoke(opener)
        self.assertEqual(status, 0)
        self.assertIn('Account: "Test User"', output)
        self.assertEqual(error, "")
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://photos.example/api/users/me")
        self.assertEqual(request.get_header("X-api-key"), self.key)
        self.assertIsNone(request.data)

    def test_errors_do_not_expose_response_bodies_or_secrets(self):
        for code in (301, 401, 403, 404, 500):
            with self.subTest(code=code):
                opener = MagicMock()
                opener.open.side_effect = HTTPError("https://photos.example", code,
                                                    self.key, {}, io.BytesIO(self.key.encode()))
                status, output, error = self.invoke(opener)
                self.assertEqual(status, 1)
                self.assertEqual(output, "")
                self.assertIn("Error:", error)

    def test_invalid_json_and_network_failure(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = self.key.encode()
        self.assertEqual(self.invoke(opener)[0], 1)
        opener.open.side_effect = URLError(self.key)
        self.assertEqual(self.invoke(opener)[0], 1)

    def test_malformed_config_never_prints_its_contents(self):
        self.path.write_text(self.key)
        opener = MagicMock()
        self.assertEqual(self.invoke(opener)[0], 1)
        opener.open.assert_not_called()

    def test_url_normalisation_and_validation(self):
        for url in ("https://photos.example", "https://photos.example/", "https://photos.example/api"):
            self.path.write_text(f"[immich]\nserver={url}\napi_key={self.key}\n")
            self.assertEqual(ping.load_config(self.path)[0], "https://photos.example/api/users/me")
        for url in ("", "file:///etc/passwd", "https://name:password@example.com", "https://example.com?key=secret"):
            self.path.write_text(f"[immich]\nserver={url}\napi_key={self.key}\n")
            with self.assertRaises(ping.PingError):
                ping.load_config(self.path)

    def test_redirect_handler_refuses_to_forward(self):
        self.assertIsNone(ping.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.example"))


if __name__ == "__main__":
    unittest.main()
