"""Scoro overcharge calculator — Scoro API v2 client (stdlib only, Lambda-friendly).

Validated facts baked in:
- Scoro is behind Cloudflare: requests MUST send a browser User-Agent or get
  `error code: 1010` / HTTP 403.
- Auth envelope is apiKey + company_account_id + lang in the JSON POST body.
- detailed_response caps lists at 25 per page -> paginate.
- Rate limit: 429 with x-ratelimit-* / Retry-After headers -> back off and retry.
"""

import http.client
import json
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


# ---- constants --------------------------------------------------------------

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---- errors -----------------------------------------------------------------

class ScoroError(Exception):
    pass


# ---- rate limiting ----------------------------------------------------------

class _RateLimiter:
    """Global request pacer shared across worker threads.

    Scoro returns 429 above ~25-80 req/2s (plan-dependent). The threaded fetch
    can burst well past that, so every request reserves the next slot at least
    ``1/rate`` seconds after the previous one. This caps the whole process at
    ``rate`` req/s regardless of how many threads are active.
    """

    def __init__(self, rate_per_sec):
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec and rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if wait > 0:
            time.sleep(wait)


# ---- client -----------------------------------------------------------------

class ScoroClient:
    """Thin wrapper around Scoro's JSON POST API."""

    def __init__(
        self,
        api_key,
        company_account_id,
        lang="eng",
        max_retries=8,
        timeout=30,
        reqs_per_sec=20,
    ):
        self.api_key = api_key
        self.company_account_id = company_account_id
        self.base_url = f"https://{company_account_id}.scoro.com/api/v2"
        self.lang = lang
        self.max_retries = max_retries
        self.timeout = timeout
        # Shared across all threads using this client instance.
        self._limiter = _RateLimiter(reqs_per_sec)

    # -- low-level request -----------------------------------------------------

    def _post(
        self,
        path,
        request=None,
        filter=None,
        page=None,
        per_page=None,
        detailed_response=False,
    ):
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = {
            "apiKey": self.api_key,
            "company_account_id": self.company_account_id,
            "lang": self.lang,
        }
        # List endpoints filter via a TOP-LEVEL `filter` object (NOT `request`).
        # Putting filters in `request` is silently ignored by Scoro.
        if filter:
            body["filter"] = filter
        if request is not None:
            body["request"] = request
        if detailed_response:
            body["detailed_response"] = True
        if page is not None:
            body["page"] = page
        if per_page is not None:
            body["per_page"] = per_page

        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": BROWSER_UA}

        last_err = None
        for attempt in range(self.max_retries):
            self._limiter.acquire()  # global pacing to stay under the 429 ceiling
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Honor Retry-After, fall back to exponential backoff, add
                    # jitter so concurrent threads don't retry in lockstep.
                    try:
                        retry_after = float(e.headers.get("Retry-After", ""))
                    except ValueError:
                        retry_after = 2 ** attempt
                    time.sleep(retry_after + random.uniform(0, 1))
                    last_err = e
                    continue
                raise ScoroError(
                    f"HTTP {e.code} from {path}: {e.read()[:300]!r}"
                ) from e
            except (
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
                ConnectionError,
                TimeoutError,
            ) as e:
                # Scoro/Cloudflare drops long-lived or overloaded connections —
                # retry with exponential backoff (+ jitter) rather than failing.
                last_err = e
                time.sleep(2 ** attempt + random.uniform(0, 1))
                continue

            if isinstance(payload, dict) and payload.get("status") == "ERROR":
                raise ScoroError(
                    f"Scoro ERROR from {path}: {payload.get('messages')}"
                )
            return payload

        raise ScoroError(f"Exhausted retries for {path}: {last_err}")

    # -- public API ------------------------------------------------------------

    def view(self, entity, entity_id, request=None):
        """GET-equivalent single record, e.g. retainers/view/{id}."""
        return self._post(f"{entity}/view/{entity_id}", request=request)

    def list_all(
        self,
        entity,
        filter=None,
        request=None,
        detailed_response=False,
        per_page=25,
        max_pages=10000,
    ):
        """Paginate a list endpoint to exhaustion.

        Filtering uses the top-level `filter` object. With detailed_response the
        server caps at 25/page regardless of per_page, so default to 25 and stop
        when a short/empty page comes back.
        """
        results = []
        page = 1
        while page <= max_pages:
            payload = self._post(
                f"{entity}/list",
                filter=filter,
                request=request,
                page=page,
                per_page=per_page,
                detailed_response=detailed_response,
            )
            batch = payload.get("data", payload) if isinstance(payload, dict) else payload
            if not batch:
                break
            results.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return results

    def _fetch_page(self, entity, page, filter=None, per_page=25, detailed_response=False):
        """Fetch a single list page and return its rows (list)."""
        payload = self._post(
            f"{entity}/list",
            filter=filter,
            page=page,
            per_page=per_page,
            detailed_response=detailed_response,
        )
        batch = payload.get("data", payload) if isinstance(payload, dict) else payload
        return batch or []

    def list_all_parallel(
        self,
        entity,
        filter=None,
        detailed_response=False,
        per_page=25,
        window=8,
        max_pages=10000,
    ):
        """Paginate a list endpoint with concurrent page fetches.

        ``detailed_response`` caps Scoro at 25 rows/page, so a large entity (tasks)
        needs many pages. Fetching them one-at-a-time is what blows the Lambda
        timeout, so we pull ``window`` pages at a time in a thread pool and stop
        once a short/empty page (< per_page rows) shows the end. Threads are safe:
        the client holds only immutable config and each call builds its own Request.
        Row order is not preserved (callers here group by id, so it doesn't matter).
        """
        first = self._fetch_page(entity, 1, filter, per_page, detailed_response)
        results = list(first)
        if len(first) < per_page:
            return results

        next_page = 2
        stop = False
        while not stop and next_page <= max_pages:
            pages = list(range(next_page, next_page + window))
            with ThreadPoolExecutor(max_workers=window) as pool:
                batches = list(
                    pool.map(
                        lambda p: self._fetch_page(
                            entity, p, filter, per_page, detailed_response
                        ),
                        pages,
                    )
                )
            for rows in batches:
                results.extend(rows)
                if len(rows) < per_page:
                    stop = True  # reached the last page in this window
            next_page += window
        return results

    def modify(self, entity, entity_id, fields):
        """Writes back calculated overcharge value to project's custom overcharge field."""
        return self._post(f"{entity}/modify/{entity_id}", request=fields)