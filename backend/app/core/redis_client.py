"""DocuFlow - Redis: cache, rate limiting, pub/sub, distributed locks
   Falls back to in-memory store when Redis is unavailable."""
import json
import asyncio
import time
from typing import Any, Optional
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis = None
_use_memory = False


# ── In-memory fallback implementation ─────────────────────────────────
class _MemoryStore:
    """Minimal Redis-compatible in-memory store for dev mode."""
    def __init__(self):
        self._data: dict[str, Any] = {}
        self._expiry: dict[str, float] = {}
        self._lists: dict[str, list] = {}
        self._zsets: dict[str, dict] = {}

    def _check_expiry(self, key: str):
        if key in self._expiry and time.time() > self._expiry[key]:
            self._data.pop(key, None)
            self._expiry.pop(key, None)

    async def ping(self):
        return True

    async def get(self, key: str) -> Optional[str]:
        self._check_expiry(key)
        return self._data.get(key)

    async def set(self, key: str, value: str, nx: bool = False, ex: int = None) -> bool:
        self._check_expiry(key)
        if nx and key in self._data:
            return False
        self._data[key] = value
        if ex:
            self._expiry[key] = time.time() + ex
        return True

    async def setex(self, key: str, ttl: int, value: str):
        self._data[key] = value
        self._expiry[key] = time.time() + ttl

    async def delete(self, *keys):
        for k in keys:
            self._data.pop(k, None)
            self._expiry.pop(k, None)

    async def incr(self, key: str, amount: int = 1):
        self._check_expiry(key)
        val = int(self._data.get(key, 0)) + amount
        self._data[key] = str(val)
        return val

    async def lpush(self, key: str, *values):
        lst = self._lists.setdefault(key, [])
        for v in values:
            lst.insert(0, v)

    async def ltrim(self, key: str, start: int, stop: int):
        if key in self._lists:
            self._lists[key] = self._lists[key][start:stop + 1]

    async def lrange(self, key: str, start: int, stop: int):
        lst = self._lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start:stop + 1]

    async def expire(self, key: str, ttl: int):
        self._expiry[key] = time.time() + ttl

    async def zremrangebyscore(self, key: str, min_score, max_score):
        zset = self._zsets.get(key, {})
        self._zsets[key] = {m: s for m, s in zset.items() if not (min_score <= s <= max_score)}

    async def zadd(self, key: str, mapping: dict):
        zset = self._zsets.setdefault(key, {})
        zset.update(mapping)

    async def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    def pipeline(self):
        return _MemoryPipeline(self)


class _MemoryPipeline:
    def __init__(self, store: _MemoryStore):
        self._store = store
        self._calls: list = []

    def zremrangebyscore(self, key, mn, mx):
        self._calls.append(("zremrangebyscore", key, mn, mx))
        return self

    def zadd(self, key, mapping):
        self._calls.append(("zadd", key, mapping))
        return self

    def zcard(self, key):
        self._calls.append(("zcard", key))
        return self

    def expire(self, key, ttl):
        self._calls.append(("expire", key, ttl))
        return self

    def lpush(self, key, *values):
        self._calls.append(("lpush", key, *values))
        return self

    def ltrim(self, key, start, stop):
        self._calls.append(("ltrim", key, start, stop))
        return self

    async def execute(self):
        results = []
        for call in self._calls:
            method = getattr(self._store, call[0])
            r = await method(*call[1:])
            results.append(r)
        return results


_memory_store = _MemoryStore()


async def init_redis():
    global _redis, _use_memory
    if not settings.REDIS_URL:
        logger.info("Redis URL not configured — using in-memory fallback")
        _redis = _memory_store
        _use_memory = True
        return
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        await _redis.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning(f"Redis unavailable ({e}) — using in-memory fallback")
        _redis = _memory_store
        _use_memory = True


async def get_redis():
    if _redis is None:
        return _memory_store
    return _redis


# ── Result Cache ────────────────────────────────────────────────────────
async def cache_result(doc_id: str, result: dict, ttl: int = None):
    ttl = ttl or settings.REDIS_RESULT_TTL
    await _redis.setex(f"result:{doc_id}", ttl, json.dumps(result, default=str))


async def get_cached_result(doc_id: str) -> Optional[dict]:
    data = await _redis.get(f"result:{doc_id}")
    return json.loads(data) if data else None


async def delete_result(doc_id: str):
    await _redis.delete(f"result:{doc_id}")


# ── Rate Limiting (sliding window) ─────────────────────────────────────
async def check_rate_limit(key: str, limit: int, window: int = 60) -> tuple[bool, int]:
    """Returns (allowed, remaining). Uses Redis sliding window."""
    pipe = _redis.pipeline()
    now = asyncio.get_event_loop().time()
    window_start = now - window
    full_key = f"rl:{key}"
    pipe.zremrangebyscore(full_key, 0, window_start)
    pipe.zadd(full_key, {str(now): now})
    pipe.zcard(full_key)
    pipe.expire(full_key, window)
    results = await pipe.execute()
    count = results[2]
    allowed = count <= limit
    remaining = max(0, limit - count)
    return allowed, remaining


# ── Distributed Lock ────────────────────────────────────────────────────
class RedisLock:
    def __init__(self, name: str, timeout: int = 30):
        self.key = f"lock:{name}"
        self.timeout = timeout
        self._token = None

    async def __aenter__(self):
        import uuid
        self._token = str(uuid.uuid4())
        deadline = asyncio.get_event_loop().time() + self.timeout
        while asyncio.get_event_loop().time() < deadline:
            ok = await _redis.set(self.key, self._token, nx=True, ex=self.timeout)
            if ok:
                return self
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Could not acquire lock: {self.key}")

    async def __aexit__(self, *args):
        val = await _redis.get(self.key)
        if val == self._token:
            await _redis.delete(self.key)


# ── Health ──────────────────────────────────────────────────────────────
async def check_redis_health() -> bool:
    try:
        r = _redis or _memory_store
        return await r.ping()
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return False


# ── Stats tracking ──────────────────────────────────────────────────────
async def increment_stat(key: str, amount: int = 1):
    await _redis.incr(f"stats:{key}", amount)


async def get_stat(key: str) -> int:
    v = await _redis.get(f"stats:{key}")
    return int(v) if v else 0


async def record_processing_time(ms: int):
    pipe = _redis.pipeline()
    pipe.lpush("stats:latencies", ms)
    pipe.ltrim("stats:latencies", 0, 999)   # Keep last 1000
    await pipe.execute()


async def get_avg_latency() -> float:
    vals = await _redis.lrange("stats:latencies", 0, -1)
    if not vals:
        return 0.0
    return sum(int(v) for v in vals) / len(vals)
