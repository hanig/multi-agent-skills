#!/usr/bin/env python3
"""A small standard-library client for Linear's GraphQL API.

Owner decision, 2026-10-04: tracker work goes through the API with
`LINEAR_API_KEY` rather than a session's Linear MCP connector. This module is
network-capable on purpose. `linear_sync.py` uses the client; `merge_unit.py`
uses key loading and output redaction for its operator subprocess. The
coordinator (`swarm.py`) and the offline draft tools never import it.

Every request leaves through `transport()`, the one network seam; tests
replace that single function.

The key is read from `LINEAR_API_KEY`, or, when that is unset, from
`~/.config/hanig/linear.env` so cron and non-login shells work. It is sent only
as the Authorization header, and every error message passes through
`redact()` before anyone sees it.
"""

import hashlib
import json
import os
import shlex
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.linear.app/graphql"
KEY_ENV = "LINEAR_API_KEY"
KEY_FILE = Path("~/.config/hanig/linear.env")
TIMEOUT = 60


class LinearError(RuntimeError):
    """A request failed. The message is already redacted."""


def redact(text, key):
    text = str(text)
    if key:
        # The raw key, and the form json.dumps gives it inside a string.
        for form in {key, json.dumps(key)[1:-1]}:
            text = text.replace(form, "[REDACTED]")
    return text


def _key_from_file(path):
    path = Path(path).expanduser()
    try:
        info = os.stat(path)
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise LinearError(
                'key file %s must be owned by the current user with mode 600; '
                'fix ownership if needed and run chmod 600 %s'
                % (path, shlex.quote(str(path))))
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        # Shell word splitting, so quotes and a trailing `# comment` are read
        # the way the shell that sources this file reads them.
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if words and words[0] == "export":
            words = words[1:]
        if len(words) != 1:
            continue
        name, sep, value = words[0].partition("=")
        if sep and name == KEY_ENV:
            return value or None
    return None


def load_key(environ=None, path=KEY_FILE):
    """The key, or None. Never logged; callers pass it straight to Client."""
    environ = os.environ if environ is None else environ
    key = (environ.get(KEY_ENV) or "").strip()
    return key or _key_from_file(path)


def derived_id(name):
    """A stable id in UUID v4 format for a stable name.

    SHA-256 of the name, with the version nibble set to 4 and the variant bits
    to 10, so Linear accepts it as a client-supplied id and the same name
    always yields the same id on every host.
    """
    raw = bytearray(hashlib.sha256(name.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    h = raw.hex()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def transport(body, headers, timeout=TIMEOUT):
    """The one network call. Returns (http_status, response_bytes)."""
    request = urllib.request.Request(ENDPOINT, data=body, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Client:
    def __init__(self, key):
        if not key:
            raise LinearError(
                "no Linear API key: set %s or write it to %s (mode 600)"
                % (KEY_ENV, KEY_FILE))
        self._key = key

    def __repr__(self):
        return "Client(key=[REDACTED])"

    def request(self, query, variables=None):
        """Run one GraphQL document. Returns (data, errors)."""
        body = json.dumps({"query": query,
                           "variables": variables or {}}).encode("utf-8")
        headers = {"Authorization": self._key,
                   "Content-Type": "application/json"}
        try:
            status, raw = transport(body, headers)
        except Exception as exc:  # noqa: BLE001  any transport failure
            raise LinearError("Linear request failed: %s"
                              % redact(exc, self._key))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            raise LinearError("Linear returned HTTP %s with a body that is "
                              "not JSON" % status)
        if not isinstance(payload, dict):
            raise LinearError("Linear returned HTTP %s with a non-object "
                              "body" % status)
        errors = payload.get("errors") or []
        messages = [redact((e or {}).get("message", e), self._key)
                    for e in errors if e is not None]
        if status != 200 and not messages:
            messages = ["HTTP %s" % status]
        return payload.get("data"), messages

    def query(self, query, variables=None):
        """Run a document that must succeed outright."""
        data, errors = self.request(query, variables)
        if errors or data is None:
            raise LinearError("Linear refused the request: %s"
                              % "; ".join(errors or ["no data"]))
        return data
