"""Webhook delivery with an explicit outcome.

Outcomes:
  accepted  2xx response
  retry     the request was definitely not processed (connect/DNS/TLS error,
            failure while sending, 429, 502, 503) -> safe to queue again
  unknown   the request may have been processed (timeout or disconnect while
            waiting for the response, 500, 504) -> never resent automatically
  rejected  the endpoint refused it (401/403/404/410 and other 4xx)
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.parse
from dataclasses import dataclass

RETRY_STATUSES = {429, 502, 503}
UNKNOWN_STATUSES = {500, 504}


@dataclass
class Result:
    outcome: str
    status: int = 0
    detail: str = ""


def post_json(url: str, auth: str, payload: dict, timeout: float = 20.0,
              user_agent: str = "slack-bridge/2") -> Result:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return Result("rejected", 0, "invalid webhook URL")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": user_agent,
               "Content-Length": str(len(body))}
    if auth:
        headers["Authorization"] = auth
    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=timeout,
            context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    try:
        # Phase 1: connect + send. Any failure here means the server never got
        # a complete request, so it cannot have acted on it.
        try:
            conn.connect()
            conn.request("POST", path, body=body, headers=headers)
        except (OSError, http.client.HTTPException) as exc:
            return Result("retry", 0, f"not delivered: {type(exc).__name__}")
        # Phase 2: wait for the response. A timeout or disconnect here is
        # ambiguous: the agent may already be running.
        try:
            resp = conn.getresponse()
            resp.read(65536)
        except (socket.timeout, TimeoutError) as exc:
            return Result("unknown", 0, f"timed out waiting for response ({type(exc).__name__})")
        except (OSError, http.client.HTTPException) as exc:
            return Result("unknown", 0, f"connection lost after sending ({type(exc).__name__})")
    finally:
        conn.close()
    status = resp.status
    if 200 <= status < 300:
        return Result("accepted", status)
    if status in RETRY_STATUSES:
        return Result("retry", status, f"HTTP {status}")
    if status in UNKNOWN_STATUSES or status >= 500:
        return Result("unknown", status, f"HTTP {status}")
    return Result("rejected", status, f"HTTP {status}")
