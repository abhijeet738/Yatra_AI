# app/core/cache.py
"""Caching: storage backends, plus CachedProvider, which wraps any provider with a TTL cache."""

import asyncio
import contextlib
import hashlib
import json
import time
from typing import Any, Protocol, get_type_hints

import aiosqlite
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.logging import get_logger
from app.domain.models import PriceSource

log = get_logger(__name__)

# Bump this when a model changes shape: old entries get new keys and are simply never read.
CACHE_VERSION = "v1"

# How long each kind of data stays fresh (seconds). Values from the blueprint's TTL table.
TTL_WEATHER = 3 * 3600  # forecasts update a few times a day
TTL_TRAINS = 24 * 3600  # timetables rarely change; also protects a 300/day quota
TTL_PLACES = 7 * 86_400  # places and opening hours are very stable
TTL_ROUTES = 30 * 86_400  # road distances barely change
TTL_GEO = 30 * 86_400  # a city's coordinates never really change


# ============================================================ storage backends
class Cache(Protocol):
    """A tiny key-value store. Values are JSON strings; the store never interprets them."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int) -> None: ...
    async def ping(
        self,
    ) -> None: ...  # raises if the backend is unreachable (used by /ready)
    async def close(self) -> None: ...


class MemoryCache:
    """In-process dict with expiry. Lost on restart; ideal for tests and evals."""

    def __init__(self, max_items: int = 1000) -> None:
        self._data: dict[str, tuple[float, str]] = {}  # key -> (expires_at, json)
        self._max = max_items  # cap so memory can't grow forever

    async def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():  # expired: drop it and report a miss
            del self._data[key]
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._data.pop(key, None)  # re-insert so the key counts as "newest"
        self._data[key] = (time.monotonic() + ttl, value)
        if len(self._data) > self._max:
            self._data.pop(next(iter(self._data)))  # evict the oldest entry

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        self._data.clear()


class SQLiteCache:
    """File-backed cache: survives restarts, needs no server. The default for local dev."""

    def __init__(
        self, path: str = "yatra_cache.db"
    ) -> None:  # *.db is already git-ignored
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()  # stops two first requests both opening the file

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:  # connect lazily on first use
            async with self._lock:
                if (
                    self._db is None
                ):  # re-check: another task may have connected meanwhile
                    db = await aiosqlite.connect(self.path)
                    await db.execute(
                        "CREATE TABLE IF NOT EXISTS cache "
                        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
                    )
                    await db.commit()
                    self._db = db
        return self._db

    async def get(self, key: str) -> str | None:
        db = await self._conn()
        cur = await db.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        value, expires_at = row
        if (
            expires_at < time.time()
        ):  # wall-clock time, because the file outlives the process
            await db.execute("DELETE FROM cache WHERE key = ?", (key,))
            await db.commit()
            return None
        return str(value)

    async def set(self, key: str, value: str, ttl: int) -> None:
        db = await self._conn()
        await db.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, value, time.time() + ttl),
        )
        await db.commit()

    async def ping(self) -> None:
        db = await self._conn()
        await db.execute("SELECT 1")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None


class RedisCache:
    """Shared cache for deployments with several API containers (e.g. Upstash free tier)."""

    def __init__(self, url: str) -> None:
        from redis.asyncio import (
            Redis,
        )  # lazy import: only needed when a Redis URL is set

        self._r: Any = Redis.from_url(url, decode_responses=True)  # decode bytes -> str

    async def get(self, key: str) -> str | None:
        return await self._r.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._r.set(key, value, ex=ttl)  # ex = expire after `ttl` seconds

    async def ping(self) -> None:
        await self._r.ping()

    async def close(self) -> None:
        await self._r.aclose()


async def build_cache(
    redis_url: str | None = None, sqlite_path: str = "yatra_cache.db"
) -> Cache:
    """Redis if configured and reachable; otherwise SQLite. The app never fails to start over this."""
    if redis_url:
        redis_cache = RedisCache(redis_url)
        try:
            await asyncio.wait_for(
                redis_cache.ping(), timeout=3
            )  # fail fast if unreachable
            log.info("cache_backend", backend="redis")
            return redis_cache
        except Exception as exc:
            log.warning("redis_unavailable_using_sqlite", error=repr(exc))
            with contextlib.suppress(
                Exception
            ):  # best-effort cleanup of the failed client
                await redis_cache.close()
    log.info("cache_backend", backend="sqlite")
    return SQLiteCache(sqlite_path)


# ============================================================ keys and tagging
def make_key(
    provider: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str:
    """Stable key for one call. Note: f(1, 2) and f(a=1, b=2) get different keys (harmless)."""
    # default=str turns dates etc. into text; sort_keys makes dict order irrelevant
    payload = json.dumps(
        [args, kwargs], default=str, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[
        :32
    ]  # fixed length, no odd characters
    return f"{CACHE_VERSION}:{provider}:{method}:{digest}"


def mark_source(value: Any, source: PriceSource = PriceSource.CACHED) -> Any:
    """Return a copy of `value` where every LIVE price tag became `source` (default: CACHED).

    ESTIMATED stays ESTIMATED: an estimate doesn't become more trustworthy by being stored.
    """
    if isinstance(value, PriceSource):
        return source if value is PriceSource.LIVE else value
    if isinstance(value, BaseModel):
        # Walk every field (this also reaches nested models such as Place.ticket -> Money)
        updates = {
            name: mark_source(getattr(value, name), source)
            for name in type(value).model_fields
        }
        return value.model_copy(update=updates)
    if isinstance(value, list):
        return [mark_source(v, source) for v in value]
    if isinstance(value, tuple):
        return tuple(mark_source(v, source) for v in value)
    if isinstance(value, dict):
        return {k: mark_source(v, source) for k, v in value.items()}
    return value  # numbers, strings, dates, None: nothing to tag


# ============================================================ the wrapper
class CachedProvider:
    """Wraps any provider so each of its async methods is cached.

    Typing note: methods are forwarded dynamically (__getattr__), so mypy can't see them.
    build_providers() therefore `cast`s the wrapper back to the right Protocol, in one place.
    The wrapped method MUST have a return annotation: it tells us how to rebuild the result
    from JSON. Without one the call passes through uncached (and a warning is logged).
    """

    def __init__(self, inner: Any, cache: Cache, ttl: int) -> None:
        self.inner = inner
        self.cache = cache
        self.ttl = ttl
        self.name = (
            f"cached:{inner.name}"  # shows up in logs; cache keys use the inner name
        )
        self.hits = 0  # counters feed the "cache hit rate" metric
        self.misses = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def __getattr__(self, method: str) -> Any:
        # Only called when normal lookup fails. Refuse private names and our own attributes,
        # otherwise copying/pickling this object (before __init__ ran) would recurse forever.
        if method.startswith("_") or method in {"inner", "cache", "ttl"}:
            raise AttributeError(method)
        attr = getattr(
            self.inner, method
        )  # AttributeError here if the inner has no such method
        if not callable(attr):
            return attr
        wrapped = self._wrap(method, attr)
        setattr(self, method, wrapped)  # store it, so the next access skips __getattr__
        return wrapped

    def _wrap(self, method: str, fn: Any) -> Any:
        try:
            return_type = get_type_hints(fn).get("return")  # e.g. list[DailyWeather]
        except Exception:
            return_type = None
        if return_type is None:
            log.warning(
                "cache_skipped_no_return_annotation",
                provider=self.inner.name,
                method=method,
            )
            return fn  # pass through uncached

        adapter: TypeAdapter[Any] = TypeAdapter(
            return_type
        )  # JSON <-> typed Python objects
        provider = self.inner.name

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = make_key(provider, method, args, kwargs)
            raw = await self._read(key)
            if raw is not None:
                try:
                    value = adapter.validate_json(
                        raw
                    )  # rebuild real models, not plain dicts
                except ValidationError:
                    # Stored shape no longer matches the models (e.g. after a code change)
                    log.warning("cache_entry_invalid", provider=provider, method=method)
                else:
                    self.hits += 1
                    log.debug("cache_hit", provider=provider, method=method)
                    return mark_source(value)  # live -> cached on the way out
            self.misses += 1
            log.debug("cache_miss", provider=provider, method=method)
            result = await fn(*args, **kwargs)  # errors propagate and are never cached
            await self._write(key, adapter, result)
            return result  # a fresh result keeps its own (live) tags

        return wrapper

    async def _read(self, key: str) -> str | None:
        try:
            return await self.cache.get(key)
        except Exception as exc:  # a cache outage must never break a request
            log.warning("cache_read_failed", error=repr(exc))
            return None

    async def _write(self, key: str, adapter: TypeAdapter[Any], value: Any) -> None:
        try:
            await self.cache.set(key, adapter.dump_json(value).decode(), self.ttl)
        except Exception as exc:
            log.warning("cache_write_failed", error=repr(exc))
