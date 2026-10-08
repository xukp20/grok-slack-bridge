"""Webhook delivery with an explicit outcome.

Outcomes:
  accepted  2xx response
  retry     the request was definitely not processed (connect/DNS/TLS error,
            failure while sending) -> safe to queue again
  busy      the endpoint answered but did not take the run now (400, 408, 409,
            425, 429, any 5xx). Typical cause: a Grok routine still busy with
            the previous message rejects an overlapping run. Queued again with
            a slow backoff (webhook_busy_*), in thread order.
  unknown   no answer after the request was sent (timeout, disconnect while
            waiting) -> the agent may be running it; never resent automatically
  rejected  the endpoint refused it for good: 401/403 (stale Authorization),
            404/410 (stale URL), other 4xx
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.parse
from dataclasses import dataclass

BUSY_STATUSES = {400, 408, 409, 425, 429}
CREDENTIAL_STATUSES = {401, 403}
STALE_URL_STATUSES = {404, 410}


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
    return classify_status(status)


def classify_status(status: int) -> Result:
    """Outcome for an HTTP response status (the response did arrive)."""
    if 200 <= status < 300:
        return Result("accepted", status)
    if status in BUSY_STATUSES or status >= 500:
        return Result("busy", status, f"HTTP {status}")
    if status in CREDENTIAL_STATUSES:
        return Result("rejected", status, f"HTTP {status} (Authorization rejected)")
    if status in STALE_URL_STATUSES:
        return Result("rejected", status, f"HTTP {status} (webhook URL not found)")
    return Result("rejected", status, f"HTTP {status}")
