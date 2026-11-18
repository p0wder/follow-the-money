"""Tiny Graph API client (stdlib only) with paging, retries and token redaction."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

BASE = "https://graph.facebook.com"
USER_AGENT = "follow-the-money/0.1 (public-interest research prototype)"


class GraphError(RuntimeError):
    pass


def _redact(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [
        (k, "REDACTED" if k == "access_token" else v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


class GraphClient:
    def __init__(self, access_token: str, version: str = "v23.0", *, max_retries: int = 4,
                 timeout: float = 30.0, opener=None):
        if not access_token:
            raise GraphError("META_ACCESS_TOKEN is not set")
        self.token = access_token
        self.version = version
        self.max_retries = max_retries
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def _url(self, path: str, params: dict[str, Any]) -> str:
        params = {k: v for k, v in params.items() if v is not None}
        params["access_token"] = self.token
        return f"{BASE}/{self.version}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"

    def _fetch(self, url: str) -> dict:
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                retryable = exc.code in (429, 500, 502, 503, 504)
                if retryable and attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                try:
                    msg = json.loads(body).get("error", {}).get("message", body)
                except ValueError:
                    msg = body
                raise GraphError(f"HTTP {exc.code} for {_redact(url)}: {msg}") from None
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GraphError(f"Network error for {_redact(url)}: {exc.reason}") from None
        raise GraphError("unreachable")

    def get(self, path: str, **params: Any) -> dict:
        return self._fetch(self._url(path, params))

    def paginate(self, path: str, *, max_pages: int = 10, **params: Any) -> Iterator[dict]:
        """Yield items from `data` across pages, following `paging.next`."""
        url: str | None = self._url(path, params)
        pages = 0
        while url and pages < max_pages:
            payload = self._fetch(url)
            yield from payload.get("data", [])
            url = payload.get("paging", {}).get("next")
            pages += 1
