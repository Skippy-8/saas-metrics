"""SEC EDGAR HTTP client: a shared token-bucket rate limit, gzip, and Retry-After-aware backoff.

Adapted from quantlab (quantlab/sec/client.py). The contact is read from SEC_USER_AGENT,
which may be set in a local, git-ignored .env file.

SEC fair-access policy: at most 10 requests a second, and a User-Agent naming a
real contact. The contact is read from ``SEC_USER_AGENT`` (for example
``"Jane Doe jane@example.com"``); the client refuses to run without one rather
than send an anonymous agent that SEC would block.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request

RATE_PER_S = 8.0                  # under SEC's 10/s ceiling
MAX_RETRIES = 4
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0


class TokenBucket:
    def __init__(self, rate: float = RATE_PER_S, burst: int = 8):
        self.rate, self.burst = rate, burst
        self._tokens, self._last = float(burst), time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait + 0.005)


LIMITER = TokenBucket()


def user_agent() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        raise RuntimeError("set SEC_USER_AGENT to 'Your Name your@email' (SEC requires a contact)")
    return ua


def fetch(url: str, retries: int = MAX_RETRIES) -> bytes:
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip",
               "Host": "data.sec.gov" if "data.sec.gov" in url else "www.sec.gov"}
    last: Exception | None = None
    for attempt in range(retries + 1):
        LIMITER.acquire()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                data = r.read()
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise
            if e.code in (429, 503):
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = float(ra) if ra and ra.isdigit() else BACKOFF_BASE_S * 2 ** attempt + random.uniform(0, .5)
                time.sleep(min(wait, BACKOFF_MAX_S))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(min(BACKOFF_BASE_S * 2 ** attempt, BACKOFF_MAX_S))
    raise RuntimeError(f"giving up on {url}: {last}")


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url))
