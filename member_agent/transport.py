import json
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import validate_endpoint
from .errors import UpstreamError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_seconds(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 1.0


class JsonTransport:
    def __init__(self):
        self.opener = build_opener(NoRedirect())

    def request(self, url, *, headers=None, body=None, timeout=5):
        validate_endpoint(url.split("?", 1)[0])
        req = Request(url, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
        try:
            with self.opener.open(req, timeout=max(0.01, timeout)) as response:
                # Bound memory even when a dependency violates its contract.
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise UpstreamError(502)
                return json.loads(raw)
        except HTTPError as exc:
            delay = retry_seconds(exc.headers.get("Retry-After", "1"))
            exc.close()
            raise UpstreamError(exc.code, delay) from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise UpstreamError(503, 0.1) from None
        except (ValueError, UnicodeError):
            raise UpstreamError(502) from None
