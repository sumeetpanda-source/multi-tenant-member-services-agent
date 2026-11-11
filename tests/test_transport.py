import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from member_agent.config import validate_endpoint
from member_agent.errors import AgentError, UpstreamError
from member_agent.transport import JsonTransport, retry_seconds


class TransportTests(unittest.TestCase):
    def test_redirects_cannot_forward_credentials(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/credential-sink")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(UpstreamError) as error:
                JsonTransport().request(f"http://127.0.0.1:{server.server_port}/start",
                                        headers={"Authorization": "Bearer test-secret"})
            self.assertEqual(302, error.exception.status)
            self.assertEqual(["/start"], paths)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_nonlocal_plaintext_and_embedded_credentials_are_rejected(self):
        for url in ("http://api.invalid", "https://user:password@api.invalid", "file:///tmp/test",
                    "https://api.invalid?secret=abc", "https://api.invalid#fragment"):
            with self.subTest(url=url):
                with self.assertRaises(AgentError):
                    validate_endpoint(url)

    def test_retry_after_parsing(self):
        self.assertEqual(2.5, retry_seconds("2.5"))
        self.assertEqual(0, retry_seconds("-1"))
        self.assertEqual(1, retry_seconds("invalid"))


if __name__ == "__main__":
    unittest.main()
