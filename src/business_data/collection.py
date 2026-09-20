"""Bounded asynchronous GET collection, retries, host pacing, and memory cache."""

import asyncio
from dataclasses import dataclass, field
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


@dataclass(frozen=True)
class Response:
    status: int
    body: str
    headers: dict = field(default_factory=dict)


class FixtureTransport:
    """Exact offline URL lookup; a sequence can simulate transient failures."""

    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.calls = {}

    async def __call__(self, url):
        self.calls[url] = self.calls.get(url, 0) + 1
        sequence = self.fixtures.get(url)
        if not sequence:
            return Response(404, "")
        item = sequence[min(self.calls[url] - 1, len(sequence) - 1)]
        body = item.get("body", "")
        return Response(item["status"], body if isinstance(body, str) else json.dumps(body), item.get("headers", {}))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """Opt-in HTTPS GET transport for explicitly approved hosts; no redirects.

    No authentication, cookies, or persistent cache is used. The caller must
    check access terms and crawl permissions before enabling live collection.
    """

    def __init__(self, allowed_hosts, user_agent, timeout=15, max_bytes=2_000_000):
        if not allowed_hosts or not user_agent.strip():
            raise ValueError("Live transport needs allowed hosts and an identifying user agent")
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.user_agent, self.timeout, self.max_bytes = user_agent, timeout, max_bytes

    async def __call__(self, url):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self.allowed_hosts or parts.username or parts.password or parts.port not in (None, 443):
            raise ValueError("URL is outside the approved HTTPS host list")

        def request():
            req = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json,text/html"})
            try:
                with build_opener(NoRedirect()).open(req, timeout=self.timeout) as response:
                    body = response.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise ValueError("Response exceeds size limit")
                    return Response(response.status, body.decode("utf-8"), dict(response.headers))
            except HTTPError as exc:
                return Response(exc.code, "", dict(exc.headers))
            except (URLError, TimeoutError, OSError) as exc:
                raise ConnectionError("Network request failed") from exc
        return await asyncio.to_thread(request)


class HostLimiter:
    def __init__(self, interval=0.2, clock=time.monotonic, sleep=asyncio.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self.locks, self.next_time = {}, {}

    async def wait(self, host):
        async with self.locks.setdefault(host, asyncio.Lock()):
            delay = max(0.0, self.next_time.get(host, 0) - self.clock())
            if delay:
                await self.sleep(delay)
            self.next_time[host] = self.clock() + self.interval


class Collector:
    def __init__(self, transport, concurrency=4, interval=0.2, attempts=3,
                 backoff=0.1, ttl=300, clock=time.monotonic, sleep=asyncio.sleep):
        if concurrency < 1 or attempts < 1 or min(interval, backoff, ttl) < 0:
            raise ValueError("Invalid collection configuration")
        self.transport, self.attempts, self.backoff, self.ttl = transport, attempts, backoff, ttl
        self.clock, self.sleep = clock, sleep
        self.semaphore = asyncio.Semaphore(concurrency)
        self.limiter = HostLimiter(interval, clock, sleep)
        self.cache, self.locks = {}, {}
        self.stats = {"requests": 0, "retries": 0, "cache_hits": 0}

    async def get(self, url):
        async with self.locks.setdefault(url, asyncio.Lock()):
            cached = self.cache.get(url)
            if cached and cached[0] > self.clock():
                self.stats["cache_hits"] += 1
                return cached[1]
            for attempt in range(self.attempts):
                async with self.semaphore:
                    await self.limiter.wait(urlsplit(url).netloc.lower())
                    self.stats["requests"] += 1
                    try:
                        response = await self.transport(url)
                    except (ConnectionError, TimeoutError):
                        response = Response(503, "")
                if response.status == 200:
                    self.cache[url] = (self.clock() + self.ttl, response)
                    return response
                retryable = response.status in {408, 429, 500, 502, 503, 504}
                if not retryable or attempt == self.attempts - 1:
                    return response
                headers = {k.lower(): v for k, v in response.headers.items()}
                try:
                    retry_after = max(0, float(headers.get("retry-after", 0)))
                except (ValueError, TypeError):
                    return response  # Do not retry early when the delay is unknown.
                delay = max(retry_after, self.backoff * 2 ** attempt)
                if not math.isfinite(delay) or delay > 60:
                    return response  # Required delay exceeds this run's wait budget.
                self.stats["retries"] += 1
                await self.sleep(delay)
