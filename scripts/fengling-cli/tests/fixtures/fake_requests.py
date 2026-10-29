"""A no-network Suno API boundary for the cached-login CLI test."""

import os
import email.utils
import time
from pathlib import Path


class RequestException(Exception):
    pass


class Response:
    text = '{"clips": [], "has_more": false}'
    def __init__(self, status_code=200, payload=None, invalid_json=False, headers=None):
        self.status_code = status_code
        self.ok = status_code == 200
        self.payload = payload
        self.invalid_json = invalid_json
        self.headers = headers or {}

    def json(self):
        if self.invalid_json:
            raise ValueError("not JSON")
        if self.payload is not None:
            return self.payload
        return {"clips": [], "has_more": False}


class Session:
    def request(self, *args, **kwargs):
        return Response()


def get(url, headers=None, timeout=None, allow_redirects=True):
    marker = os.environ.get("FENGLING_TEST_PROBE_MARKER")
    count = 1
    if marker:
        path = Path(marker)
        count = int(path.read_text()) + 1 if path.exists() else 1
        path.write_text(str(count))
    scenario = os.environ.get("FENGLING_TEST_SCENARIO")
    if scenario == "network-error" or (scenario == "transient-network" and count == 1):
        raise RequestException("synthetic-sensitive-error")
    if scenario == "transient-503" and count == 1:
        return Response(503)
    if scenario == "transient-429" and count == 1:
        return Response(429)
    if scenario == "forbidden":
        return Response(403)
    if scenario == "permanent-503":
        return Response(503)
    if scenario == "rate-limited-long":
        return Response(429, headers={"Retry-After": "5"})
    if scenario == "rate-limited-date":
        return Response(429, headers={"Retry-After": email.utils.formatdate(time.time() + 30, usegmt=True)})
    if scenario == "rate-limited-invalid":
        return Response(429, headers={"Retry-After": "not-a-date"})
    if scenario == "html-response":
        return Response(200, invalid_json=True)
    if scenario == "wrong-json":
        return Response(200, payload={"unexpected": "page"})
    token = (headers or {}).get("Authorization", "")
    expected = "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJodHRwczovL2NsZXJrLnN1bm8uY29tIiwiZXhwIjo0MTAyNDQ0ODAwfQ.signature"
    return Response(200 if token == expected else 401)
